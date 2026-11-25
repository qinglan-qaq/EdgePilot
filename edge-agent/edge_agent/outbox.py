"""SQLite 持久化待发队列（断网补传核心）。

表名固定为 ``pending_messages``；``seq`` 自增，``flush`` 严格按 seq 升序补发。
投递语义为至少一次：成功发布后立即 ack（删除），失败则记 attempts/last_error
并停止本轮，剩余消息留待下次重连。服务端需按 ``(node_key, msg_id)`` 去重。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_DDL = """
CREATE TABLE IF NOT EXISTS pending_messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    qos        INTEGER NOT NULL,
    retain     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
)
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(value: datetime) -> str:
    """统一 UTC ISO-8601；naive datetime 按 UTC 解释。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def dumps_payload(payload: dict) -> str:
    """紧凑、稳定、不转义中文的 JSON 序列化（发布与入库共用）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class PendingMessage:
    seq: int
    topic: str
    payload: dict
    qos: int
    retain: bool
    created_at: str
    attempts: int


@dataclass(frozen=True)
class FlushResult:
    sent: int
    remaining: int
    error: str | None = None


class Outbox:
    """线程安全的单进程 SQLite 队列（一个事件循环 + paho 回调线程足够）。"""

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or utcnow
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_DDL)
            self._conn.commit()

    # -- 入队 ---------------------------------------------------------------
    def _prepare(self, payload: dict | str) -> tuple[str, dict]:
        """返回 (入库文本, 待发 dict)；dict 载荷补全 msg_id/sent_at。"""
        if isinstance(payload, dict):
            enriched = dict(payload)
            enriched.setdefault("msg_id", uuid.uuid4().hex)
            enriched.setdefault("sent_at", to_utc_iso(self._now()))
            return dumps_payload(enriched), enriched
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return payload, parsed
            wrapped = {"raw": payload}
            return dumps_payload(wrapped), wrapped
        raise TypeError(f"payload 必须是 dict 或 str，收到 {type(payload)!r}")

    def enqueue(
        self,
        topic: str,
        payload: dict | str,
        *,
        qos: int = 1,
        retain: bool = False,
    ) -> int:
        text, _ = self._prepare(payload)
        created_at = to_utc_iso(self._now())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO pending_messages (topic, payload, qos, retain, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (topic, text, int(qos), int(bool(retain)), created_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    # -- 查询 / 状态 --------------------------------------------------------
    def pending(self, limit: int = 100) -> list[PendingMessage]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, topic, payload, qos, retain, created_at, attempts "
                "FROM pending_messages ORDER BY seq ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            PendingMessage(
                seq=row["seq"],
                topic=row["topic"],
                payload=json.loads(row["payload"]),
                qos=row["qos"],
                retain=bool(row["retain"]),
                created_at=row["created_at"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM pending_messages").fetchone()
        return int(row["n"])

    def ack(self, seq: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_messages WHERE seq = ?", (int(seq),))
            self._conn.commit()

    def fail(self, seq: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_messages "
                "SET attempts = attempts + 1, last_error = ? WHERE seq = ?",
                (error, int(seq)),
            )
            self._conn.commit()

    # -- 补发 ---------------------------------------------------------------
    def flush(
        self,
        publish: Callable[[PendingMessage], None],
        *,
        limit: int = 100,
    ) -> FlushResult:
        """按 seq 升序补发；成功后 ack，异常则记录并停止本轮。"""
        sent = 0
        for message in self.pending(limit):
            try:
                publish(message)
            except Exception as exc:  # noqa: BLE001 - 任何发布异常都要保留消息
                error = str(exc) or exc.__class__.__name__
                self.fail(message.seq, error)
                return FlushResult(sent=sent, remaining=self.count(), error=error)
            self.ack(message.seq)
            sent += 1
        return FlushResult(sent=sent, remaining=self.count(), error=None)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
