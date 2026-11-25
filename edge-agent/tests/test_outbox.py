"""Task 17 失败测试：断网 5 分钟后恢复，事件不重不丢。

覆盖：SQLite outbox 持久化/按 seq 补发、失败重试不重复、MQTT 断网入队与重连补发、
5s 心跳载荷、注册消息幂等字段、指数退避。全部使用假 paho 客户端，不碰网络。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from edge_agent.config import AgentConfig
from edge_agent.heartbeat import HeartbeatPublisher
from edge_agent.mqtt_client import MqttClient, reconnect_delay
from edge_agent.outbox import Outbox
from edge_agent.registry import build_register_payload, register
from edge_agent.simulator import simulated_event_payload
from edge_agent.topics import (
    cmd_filter,
    event_topic,
    heartbeat_topic,
    register_topic,
    status_topic,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """可手动推进的测试时钟。"""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakePaho:
    """最小 paho.Client 假实现：记录 will/publish/subscribe。"""

    def __init__(self) -> None:
        self.will = None
        self.published: list[dict] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.reconnect_delay: tuple[int, int] | None = None
        self.connected = False
        self.loop_started = False

    def will_set(self, topic, payload=None, qos=0, retain=False):  # noqa: D401
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delay = (min_delay, max_delay)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )
        return (0, len(self.published))

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return (0, 1)

    def connect(self, host, port, keepalive=60):
        self.connected = True
        return 0

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_started = False

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected


def _config(tmp_path, node_key: str = "node-1") -> AgentConfig:
    return AgentConfig(
        node_key=node_key,
        mqtt_host="broker.test",
        mqtt_port=1883,
        outbox_path=tmp_path / "outbox.sqlite3",
    )


def _client(tmp_path, node_key: str = "node-1") -> tuple[FakePaho, Outbox, MqttClient]:
    fake = FakePaho()
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    config = _config(tmp_path, node_key)
    client = MqttClient(config, outbox, client_factory=lambda client_id: fake)
    return fake, outbox, client


# ---------------------------------------------------------------------------
# 1. 断网 5 分钟：不丢、不重、跨进程恢复、严格按 seq
# ---------------------------------------------------------------------------


def test_offline_events_survive_five_minute_outage_and_replay_once(tmp_path):
    clock = Clock(T0)
    path = tmp_path / "outbox.sqlite3"
    topic = event_topic("node-1")

    outbox = Outbox(path, now=clock)
    original_ids = []
    for i in range(1, 4):
        msg_id = f"evt-{i}"
        original_ids.append(msg_id)
        outbox.enqueue(
            topic,
            {"msg_id": msg_id, "sent_at": clock().isoformat(), "kind": "mock_event"},
        )

    # 断网 5 分钟，期间事件继续产生（第 4 条不预先带 msg_id，验证入队补全）。
    clock.advance(5 * 60)
    outbox.enqueue(topic, {"kind": "mock_event", "simulated": True})
    assert outbox.count() == 4
    outbox.close()

    # 重启（重开 SQLite）后待发消息仍在，seq 严格递增 1..4。
    reopened = Outbox(path, now=clock)
    pending = reopened.pending()
    assert [m.seq for m in pending] == [1, 2, 3, 4]
    assert [m.seq for m in pending] == sorted(m.seq for m in pending)
    assert reopened.count() == 4

    fourth = pending[3].payload
    assert fourth["msg_id"]
    assert fourth["sent_at"] == (T0 + timedelta(minutes=5)).isoformat()

    sent = []
    result = reopened.flush(sent.append)
    assert result.sent == 4
    assert result.remaining == 0
    assert result.error is None

    # 恢复后按 seq 升序恰好各发一次，msg_id 与断网时一致。
    assert [m.seq for m in sent] == [1, 2, 3, 4]
    assert [m.payload for m in sent] == [m.payload for m in pending]
    assert [m.payload["msg_id"] for m in sent[:3]] == original_ids
    assert reopened.count() == 0

    # 再 flush 一次不得重发任何已 ack 的消息。
    again = []
    second = reopened.flush(again.append)
    assert second.sent == 0
    assert again == []
    reopened.close()


# ---------------------------------------------------------------------------
# 2. 发布失败：当前及后续保留，已 ack 的不重复
# ---------------------------------------------------------------------------


def test_failed_publish_keeps_current_and_remaining_without_duplicating_acked(tmp_path):
    clock = Clock(T0)
    outbox = Outbox(tmp_path / "outbox.sqlite3", now=clock)
    for i in range(1, 5):
        outbox.enqueue(event_topic("node-1"), {"msg_id": f"evt-{i}"})

    attempts = []
    acked = []

    def flaky_publish(msg):
        attempts.append(msg)
        if len(attempts) == 3:
            raise RuntimeError("broker rejected publish")
        acked.append(msg)

    result = outbox.flush(flaky_publish)
    assert result.sent == 2
    assert result.remaining == 2
    assert result.error == "broker rejected publish"
    assert outbox.count() == 2

    # seq 1、2 已 ack；失败的 seq 3 与未处理的 seq 4 仍待发；attempts 已 +1。
    remaining = outbox.pending()
    assert [m.seq for m in remaining] == [3, 4]
    assert remaining[0].attempts == 1
    assert remaining[1].attempts == 0
    assert [m.seq for m in acked] == [1, 2]

    # 重领会略过已 ack 的 seq 1、2，从 seq 3 继续。
    retried = []
    second = outbox.flush(retried.append)
    assert second.sent == 2
    assert second.remaining == 0
    assert second.error is None
    assert [m.seq for m in retried] == [3, 4]

    all_acked_ids = [m.payload["msg_id"] for m in acked + retried]
    assert len(all_acked_ids) == 4
    assert len(set(all_acked_ids)) == 4  # 无重复投递
    outbox.close()


# ---------------------------------------------------------------------------
# 3. MQTT 离线入队，on_connect 后按 seq 补发
# ---------------------------------------------------------------------------


def test_offline_publish_enqueues_and_reconnect_flushes_in_seq_order(tmp_path):
    fake, outbox, client = _client(tmp_path)

    # 构造即设置遗嘱：retained offline，QoS 1。
    assert fake.will == (status_topic("node-1"), "offline", 1, True)
    assert fake.reconnect_delay == (1, 30)

    events = [{"msg_id": f"off-{i}", "kind": "mock_event"} for i in range(1, 4)]
    for event in events:
        assert client.publish(event_topic("node-1"), event, qos=1) is False
    assert outbox.count() == 3
    assert fake.published == []  # 断网期间没有任何真实发布

    # 模拟 paho 连接成功回调。
    fake.on_connect(fake, None, {}, 0, None)
    assert client.connected

    statuses = [p for p in fake.published if p["topic"] == status_topic("node-1")]
    assert statuses == [
        {
            "topic": status_topic("node-1"),
            "payload": "online",
            "qos": 1,
            "retain": True,
        }
    ]
    assert (cmd_filter("node-1"), 1) in fake.subscriptions

    replayed = [
        json.loads(p["payload"])
        for p in fake.published
        if p["topic"] == event_topic("node-1")
    ]
    assert [m["msg_id"] for m in replayed] == [e["msg_id"] for e in events]
    assert outbox.count() == 0


# ---------------------------------------------------------------------------
# 4. 心跳：必需字段、5s 默认、QoS 0、离线不落 outbox
# ---------------------------------------------------------------------------


def test_heartbeat_payload_has_required_fields_and_uses_5s_default(tmp_path):
    fake, outbox, client = _client(tmp_path)
    fake.on_connect(fake, None, {}, 0, None)

    metrics = {"cpu": 12.5, "mem": 41.0, "disk": 66.5, "gpu": None}
    heartbeat = HeartbeatPublisher(
        client,
        "node-1",
        metrics_fn=lambda: metrics,
        task_count_fn=lambda: 2,
        clock=lambda: T0,
    )
    assert heartbeat.interval_s == 5.0

    payload = heartbeat.build_payload()
    assert {
        "msg_id",
        "sent_at",
        "node_key",
        "cpu",
        "mem",
        "disk",
        "gpu",
        "task_count",
    } <= set(payload)
    assert payload["sent_at"] == T0.isoformat()
    assert payload["node_key"] == "node-1"
    assert payload["cpu"] == 12.5
    assert payload["mem"] == 41.0
    assert payload["disk"] == 66.5
    assert payload["gpu"] is None
    assert payload["task_count"] == 2

    assert heartbeat.tick() is True
    last = fake.published[-1]
    assert last["topic"] == heartbeat_topic("node-1")
    assert last["qos"] == 0
    assert last["retain"] is False
    assert outbox.count() == 0

    # 离线时心跳直接丢弃，不写 outbox（过期心跳无补发价值）。
    offline_fake = FakePaho()
    offline_outbox = Outbox(tmp_path / "offline.sqlite3")
    offline = MqttClient(
        _config(tmp_path),
        offline_outbox,
        client_factory=lambda client_id: offline_fake,
    )
    offline_heartbeat = HeartbeatPublisher(
        offline, "node-1", metrics_fn=lambda: metrics
    )
    assert offline_heartbeat.tick() is False
    assert offline_outbox.count() == 0
    assert offline_fake.published == []


# ---------------------------------------------------------------------------
# 5. 注册：幂等字段、主题、离线排队
# ---------------------------------------------------------------------------


def test_register_payload_idempotent_fields(tmp_path):
    first = build_register_payload(
        "node-1", os_name="Linux", arch="x86_64", agent_version="0.1.0", clock=lambda: T0
    )
    second = build_register_payload(
        "node-1", os_name="Linux", arch="x86_64", agent_version="0.1.0", clock=lambda: T0
    )

    assert first["msg_id"] != second["msg_id"]
    for key in ("node_key", "os", "arch", "agent_version"):
        assert first[key] == second[key]
    assert first["sent_at"] == T0.isoformat()
    assert register_topic("node-1") == "edge/node-1/register"

    _, outbox, client = _client(tmp_path)
    assert register(client, first) is False  # 离线，写入待发队列
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0].topic == "edge/node-1/register"
    assert pending[0].payload["msg_id"] == first["msg_id"]
    assert pending[0].qos == 1


# ---------------------------------------------------------------------------
# 6. 指数退避 1s → 30s
# ---------------------------------------------------------------------------


def test_reconnect_delay_is_exponential_capped_at_30s():
    assert [reconnect_delay(attempt) for attempt in range(7)] == [1, 2, 4, 8, 16, 30, 30]


# ---------------------------------------------------------------------------
# 7. 模拟事件必须带后端入库所需的 occurred_at，且显式标记 simulated
# ---------------------------------------------------------------------------


def test_simulated_event_payload_has_ingest_contract():
    payload = simulated_event_payload("node-1", clock=lambda: T0)

    assert payload["simulated"] is True
    assert payload["node_key"] == "node-1"
    assert payload["event_type"] == "mock_detection"
    assert payload["occurred_at"] == T0.isoformat()
    assert payload["sent_at"] == payload["occurred_at"]
    assert payload["msg_id"]
    assert "confidence" not in payload
