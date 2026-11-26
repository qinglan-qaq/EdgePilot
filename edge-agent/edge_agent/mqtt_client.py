"""paho-mqtt 2.x 薄封装：LWT、重连、状态与 outbox 补发。

设计约束：
- 构造时即设置 retained ``offline`` 遗嘱，保证异常掉线由 broker 兜底；
- ``on_connect`` 先发布 retained ``online``、订阅自身 cmd，再同步 flush outbox；
- 心跳/状态不持久化（``persist_on_failure=False``），注册/事件/task ack 持久化；
- 不打印载荷或凭据，仅记录主题与计数。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .config import AgentConfig
from .outbox import Outbox, PendingMessage, dumps_payload
from .topics import cmd_filter, status_topic

logger = logging.getLogger(__name__)

ClientFactory = Callable[..., Any]


def reconnect_delay(attempt: int, *, min_delay: float = 1.0, max_delay: float = 30.0) -> float:
    """指数退避：1,2,4,8,16,30,30…（秒）。"""
    attempt = max(0, int(attempt))
    return min(min_delay * (2**attempt), max_delay)


def _default_client_factory(client_id: str) -> Any:
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MqttClient:
    def __init__(
        self,
        config: AgentConfig,
        outbox: Outbox,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self.outbox = outbox
        self._connected = False
        self._command_handlers: dict[str, Callable[[dict], None]] = {}

        factory = client_factory or _default_client_factory
        self._client = factory(client_id=f"edge-agent-{config.node_key}")
        self._client.will_set(
            status_topic(config.node_key), payload="offline", qos=1, retain=True
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # -- 生命周期 -----------------------------------------------------------
    @property
    def client(self) -> Any:
        return self._client

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> int:
        rc = self._client.connect(
            self.config.mqtt_host, self.config.mqtt_port, keepalive=30
        )
        loop_start = getattr(self._client, "loop_start", None)
        if loop_start is not None:
            loop_start()
        return int(rc)

    def disconnect(self) -> None:
        try:
            self._client.disconnect()
        finally:
            loop_stop = getattr(self._client, "loop_stop", None)
            if loop_stop is not None:
                loop_stop()
            self._connected = False

    # -- 回调 ---------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if _reason_code_failed(reason_code):
            self._connected = False
            logger.warning("mqtt 连接被拒绝 node_key=%s reason=%s", self.config.node_key, reason_code)
            return

        self._connected = True
        logger.info("mqtt 已连接 node_key=%s", self.config.node_key)

        # retained online 覆盖上一次的 LWT offline。
        self.publish(
            status_topic(self.config.node_key),
            "online",
            qos=1,
            retain=True,
            persist_on_failure=False,
        )
        self._client.subscribe(cmd_filter(self.config.node_key), qos=1)

        # flush 按批发送（默认 100 条/批）；断网较久时待发量可能远超一批，
        # 必须循环到排空，否则一次重连只补 100 条，剩余消息要等下次重连。
        total_sent = 0
        while True:
            result = self.outbox.flush(self._publish_pending)
            total_sent += result.sent
            if result.error:
                logger.warning(
                    "outbox 补发中断 sent=%d remaining=%d error=%s",
                    total_sent,
                    result.remaining,
                    result.error,
                )
                break
            if result.remaining == 0:
                if total_sent:
                    logger.info("outbox 补发完成 sent=%d remaining=0", total_sent)
                break
            if result.sent == 0:
                # 待发非空却一条都发不出去：避免在 on_connect 回调里死循环。
                logger.warning(
                    "outbox 补发无进展 sent=%d remaining=%d",
                    total_sent,
                    result.remaining,
                )
                break

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None) -> None:
        self._connected = False
        logger.warning(
            "mqtt 已断开 node_key=%s reason=%s", self.config.node_key, reason_code
        )

    def _on_message(self, client, userdata, message) -> None:
        topic = getattr(message, "topic", "")
        prefix = cmd_filter(self.config.node_key).removesuffix("+")
        if not topic.startswith(prefix):
            return
        action = topic[len(prefix) :]
        handler = self._command_handlers.get(action)
        if handler is None:
            logger.debug("忽略未注册的 cmd action=%s", action)
            return
        raw = message.payload
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            logger.warning("cmd 载荷不是合法 JSON action=%s", action)
            return
        if not isinstance(payload, dict):
            payload = {"value": payload}
        handler(payload)

    def on_command(self, action: str, handler: Callable[[dict], None]) -> None:
        """注册下行指令处理器（Task 18 接入 probe/start_task 等）。"""
        self._command_handlers[action] = handler

    # -- 发布 ---------------------------------------------------------------
    def publish(
        self,
        topic: str,
        payload: dict | str,
        *,
        qos: int = 1,
        retain: bool = False,
        persist_on_failure: bool = True,
    ) -> bool:
        """发布消息；离线或失败时按需写入 outbox，返回是否真实发出。"""
        message = _with_envelope(payload) if isinstance(payload, dict) else payload

        if not self._connected:
            if persist_on_failure:
                self.outbox.enqueue(topic, message, qos=qos, retain=retain)
            return False

        try:
            self._publish_now(topic, message, qos, retain)
        except Exception as exc:  # noqa: BLE001 - 发布失败必须可回退到 outbox
            logger.warning("mqtt 发布失败 topic=%s error=%s", topic, exc)
            if persist_on_failure:
                self.outbox.enqueue(topic, message, qos=qos, retain=retain)
            return False
        return True

    def _publish_now(self, topic: str, payload: dict | str, qos: int, retain: bool) -> None:
        text = dumps_payload(payload) if isinstance(payload, dict) else payload
        info = self._client.publish(topic, text, qos=qos, retain=retain)
        if isinstance(info, tuple):
            rc = info[0]
        elif isinstance(info, int):
            rc = info
        else:
            rc = getattr(info, "rc", 0)
        if rc != 0:
            raise RuntimeError(f"mqtt publish rc={rc}")

    def _publish_pending(self, message: PendingMessage) -> None:
        self._publish_now(message.topic, message.payload, message.qos, message.retain)


def _with_envelope(payload: dict) -> dict:
    enriched = dict(payload)
    enriched.setdefault("msg_id", uuid.uuid4().hex)
    enriched.setdefault("sent_at", _utc_iso_now())
    return enriched


def _reason_code_failed(reason_code: Any) -> bool:
    failed = getattr(reason_code, "is_failure", None)
    if failed is not None:
        return bool(failed)
    value = getattr(reason_code, "value", reason_code)
    return value not in (0, None)
