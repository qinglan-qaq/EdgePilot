"""paho-mqtt 控制面客户端。

设计要点：
- paho 回调线程只做「拷进有界 asyncio 队列」，异步 worker 逐条处理；队列满
  直接丢弃并计数/告警，绝不让 broker 线程阻塞。
- 启动使用 ``connect_async`` + ``loop_start``，broker 不可达只记录日志，
  不阻断 API 启动。
- 无请求上下文：租户固定取 ``settings.tenant_default_id``（MVP 单租户），
  显式传给 handler。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import paho.mqtt.client as mqtt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db import get_session_factory
from app.messaging.mqtt.dedupe import MemoryDedupe, RedisDedupe
from app.messaging.mqtt.handlers import (
    handle_event,
    handle_heartbeat,
    handle_register,
    handle_status,
    handle_task_ack,
)
from app.messaging.mqtt.topics import (
    UPSTREAM_SUBSCRIPTIONS,
    cmd_topic,
    is_valid_node_key,
    parse_uplink,
)

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 1000


class MqttService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        dedupe=None,
        client_factory=None,
        tenant_id: UUID | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        client_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory or get_session_factory()
        self.tenant_id = tenant_id or UUID(self.settings.tenant_default_id)
        self.dedupe = dedupe or RedisDedupe(
            self.settings.redis_url,
            ttl_seconds=self.settings.mqtt_dedupe_ttl_seconds,
        )
        self._client_factory = client_factory or self._default_client_factory
        self._client_id = client_id or f"edgepilot-api-{uuid.uuid4().hex[:8]}"
        self.queue_size = int(queue_size)

        self._client: Any | None = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._started = False
        self._stopping = False
        self.dropped = 0

    # -- 生命周期 -----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._worker = asyncio.create_task(self._run_worker(), name="mqtt-worker")
        self._started = True

        try:
            self._client = self._client_factory(self._client_id)
            self._configure_client(self._client)
            self._client.connect_async(
                self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30
            )
            self._client.loop_start()
        except Exception:  # noqa: BLE001 - broker/网络不可用绝不能阻断 API 启动
            logger.exception("MQTT 启动失败，控制面继续以离线状态运行")

    async def stop(self) -> None:
        if not self._started and self._client is None:
            await self._close_dedupe()
            return
        self._stopping = True
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - 停机尽力而为
                logger.debug("MQTT disconnect 异常", exc_info=True)
            try:
                client.loop_stop()
            except Exception:  # noqa: BLE001
                logger.debug("MQTT loop_stop 异常", exc_info=True)
        self._connected = False

        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("MQTT worker 停止异常")

        self._queue = None
        self._loop = None
        self._started = False
        await self._close_dedupe()

    async def _close_dedupe(self) -> None:
        try:
            await self.dedupe.close()
        except Exception:  # noqa: BLE001
            logger.debug("去重存储关闭异常", exc_info=True)

    # -- paho 配置 ----------------------------------------------------------
    @staticmethod
    def _default_client_factory(client_id: str) -> Any:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)

    def _configure_client(self, client) -> None:
        if self.settings.mqtt_username:
            client.username_pw_set(
                self.settings.mqtt_username, self.settings.mqtt_password or None
            )
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if _reason_code_failed(reason_code):
            self._connected = False
            logger.warning("MQTT 连接被拒绝 reason=%s", reason_code)
            return
        self._connected = True
        try:
            client.subscribe(list(UPSTREAM_SUBSCRIPTIONS))
        except Exception:  # noqa: BLE001
            logger.exception("MQTT 订阅失败")
        logger.info(
            "MQTT 已连接 host=%s port=%s", self.settings.mqtt_host, self.settings.mqtt_port
        )

    def _on_disconnect(
        self, client, userdata, disconnect_flags=None, reason_code=None, properties=None
    ) -> None:
        self._connected = False
        logger.warning("MQTT 已断开 reason=%s", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        loop = self._loop
        if loop is None or self._stopping:
            return
        topic = getattr(message, "topic", "")
        payload = getattr(message, "payload", b"")
        try:
            loop.call_soon_threadsafe(self._enqueue, topic, payload)
        except RuntimeError:
            # loop 已关闭：停机阶段正常现象。
            return

    def _enqueue(self, topic: str, payload: bytes) -> None:
        if self._queue is None or self._stopping:
            return
        try:
            self._queue.put_nowait((topic, payload))
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning("MQTT 队列已满，丢弃消息 topic=%s dropped=%d", topic, self.dropped)

    # -- 异步处理 -----------------------------------------------------------
    async def _run_worker(self) -> None:
        while True:
            queue = self._queue
            if queue is None:
                return
            topic, payload = await queue.get()
            try:
                await self._dispatch(topic, payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单条消息失败不终止 worker
                logger.exception("MQTT 消息处理失败 topic=%s", topic)
            finally:
                queue.task_done()

    async def _dispatch(self, topic: str, payload: bytes) -> str | None:
        parsed = parse_uplink(topic)
        if parsed is None:
            logger.warning("忽略非上行主题 topic=%s", topic)
            return None
        node_key, kind = parsed
        if not is_valid_node_key(node_key):
            return None

        if kind == "status":
            value = _decode_text(payload)
            if value is None:
                return None
            if value.startswith("{"):
                parsed_json = _decode_json(value)
                if parsed_json is None:
                    return None
                status = parsed_json
            else:
                status = value
            async with self.session_factory() as session:
                try:
                    result = await handle_status(
                        session, self.tenant_id, node_key, status, self.dedupe
                    )
                except Exception:
                    await session.rollback()
                    raise
            logger.info("MQTT status node_key=%s result=%s", node_key, result)
            return result

        text = _decode_text(payload)
        if text is None:
            return None
        data = _decode_json(text)
        if data is None:
            logger.warning("MQTT 载荷不是合法 JSON 对象 topic=%s", topic)
            return None

        handler = {
            "register": handle_register,
            "heartbeat": handle_heartbeat,
            "event": handle_event,
            "task_ack": handle_task_ack,
        }[kind]
        async with self.session_factory() as session:
            try:
                result = await handler(session, self.tenant_id, node_key, data, self.dedupe)
            except Exception:
                await session.rollback()
                raise
        logger.info("MQTT %s node_key=%s result=%s", kind, node_key, result)
        return result

    # -- 下行 ---------------------------------------------------------------
    async def publish_command(
        self,
        node_key: str,
        action: str,
        payload: dict,
        qos: int = 1,
    ) -> bool:
        client = self._client
        if not self._connected or client is None:
            return False
        try:
            topic = cmd_topic(node_key, action)
        except ValueError:
            logger.warning("非法下行指令 node_key=%s action=%s", node_key, action)
            return False

        message = dict(payload or {})
        message.setdefault("msg_id", uuid.uuid4().hex)
        message.setdefault("sent_at", datetime.now(timezone.utc).isoformat())
        try:
            info = client.publish(
                topic,
                json.dumps(message, ensure_ascii=False, default=str),
                qos=qos,
                retain=False,
            )
            rc = getattr(info, "rc", 0)
            if rc != 0:
                logger.warning("MQTT 下行发布失败 topic=%s rc=%s", topic, rc)
                return False
            return True
        except Exception:  # noqa: BLE001 - 发布失败由 API 转 503
            logger.exception("MQTT 下行发布异常 topic=%s", topic)
            return False


def _decode_text(payload: bytes | bytearray | str) -> str | None:
    if isinstance(payload, str):
        return payload.strip()
    try:
        return bytes(payload).decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return None


def _decode_json(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _reason_code_failed(reason_code: Any) -> bool:
    failed = getattr(reason_code, "is_failure", None)
    if failed is not None:
        return bool(failed)
    value = getattr(reason_code, "value", reason_code)
    return value not in (0, None)


# ------------------------------------------------------------------ module API
_service: MqttService | None = None


def get_mqtt_service() -> MqttService:
    global _service
    if _service is None:
        _service = MqttService()
    return _service


async def start_mqtt_service() -> MqttService:
    service = get_mqtt_service()
    await service.start()
    return service


async def stop_mqtt_service() -> None:
    global _service
    service, _service = _service, None
    if service is not None:
        await service.stop()


def reset_mqtt_service() -> None:
    """测试用：丢弃单例（不负责断开，先调用 ``stop_mqtt_service``）。"""
    global _service
    _service = None


def get_command_publisher() -> MqttService:
    """FastAPI 依赖；测试可 override 为 fake publisher。"""
    return get_mqtt_service()


# 供测试/诊断显式构造内存去重服务。
__all__ = [
    "MqttService",
    "get_command_publisher",
    "get_mqtt_service",
    "reset_mqtt_service",
    "start_mqtt_service",
    "stop_mqtt_service",
    "MemoryDedupe",
]
