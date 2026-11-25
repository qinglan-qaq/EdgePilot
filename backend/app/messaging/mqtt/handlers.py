"""MQTT 上行消息处理。

所有 handler 都接收显式 ``session`` / ``tenant_id`` / ``node_key`` / ``payload``：
MQTT 回调线程没有 FastAPI 请求上下文，handler 绝不读取租户 ContextVar。
返回字符串便于测试与日志断言：``created`` / ``updated`` / ``duplicate`` /
``ignored`` / ``acknowledged``。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.mqtt.dedupe import build_dedupe_key
from app.messaging.mqtt.topics import is_valid_node_key
from app.models.device import Device, EdgeNode
from app.models.event import VisionEvent

logger = logging.getLogger(__name__)

CREATED = "created"
UPDATED = "updated"
DUPLICATE = "duplicate"
IGNORED = "ignored"
ACKNOWLEDGED = "acknowledged"

_ENVELOPE_KEYS = frozenset({"msg_id", "sent_at", "node_key"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _msg_id(payload: dict) -> str | None:
    value = payload.get("msg_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128:
        return None
    return value


async def _claim(dedupe, tenant_id: uuid.UUID, node_key: str, msg_id: str) -> bool:
    if dedupe is None:
        return True
    return await dedupe.try_claim(build_dedupe_key(tenant_id, node_key, msg_id))


def _is_disabled(row: EdgeNode) -> bool:
    return bool((row.labels or {}).get("disabled"))


def _optional_str(value: Any, *, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:max_len] if value else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_unique_violation(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "23505" or getattr(orig, "pgcode", None) == "23505"


async def _get_node(session: AsyncSession, tenant_id: uuid.UUID, node_key: str) -> EdgeNode | None:
    return await session.scalar(
        select(EdgeNode).where(
            EdgeNode.tenant_id == tenant_id,
            EdgeNode.node_key == node_key,
        )
    )


# --------------------------------------------------------------------- register
async def handle_register(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_key: str,
    payload: dict,
    dedupe=None,
) -> str:
    """按 ``(tenant_id,node_key)`` 幂等 upsert；首次创建 name=node_key。"""
    if not is_valid_node_key(node_key) or not isinstance(payload, dict):
        logger.warning("register ignored: 非法 node_key 或载荷")
        return IGNORED

    msg_id = _msg_id(payload)
    if msg_id is None:
        logger.warning("register ignored: 缺少合法 msg_id node_key=%s", node_key)
        return IGNORED

    payload_key = payload.get("node_key")
    if payload_key is not None and payload_key != node_key:
        logger.warning("register ignored: payload.node_key 与 topic 不一致")
        return IGNORED

    if not await _claim(dedupe, tenant_id, node_key, msg_id):
        return DUPLICATE

    os_name = _optional_str(payload.get("os"), max_len=32)
    arch = _optional_str(payload.get("arch"), max_len=32)
    agent_version = _optional_str(payload.get("agent_version"), max_len=32)
    name = _optional_str(payload.get("name"), max_len=128)
    ip = _optional_str(payload.get("ip"), max_len=64)

    for _ in range(2):
        row = await _get_node(session, tenant_id, node_key)
        if row is not None:
            if os_name is not None:
                row.os = os_name
            if arch is not None:
                row.arch = arch
            if agent_version is not None:
                row.agent_version = agent_version
            if name is not None:
                row.name = name
            if ip is not None:
                row.ip = ip
            # 被禁用的节点只能保持 disabled，注册不能复活。
            row.status = "disabled" if _is_disabled(row) else "online"
            await session.commit()
            return UPDATED

        row = EdgeNode(
            tenant_id=tenant_id,
            node_key=node_key,
            name=node_key,
            os=os_name,
            arch=arch,
            agent_version=agent_version,
            status="online",
            ip=ip,
            labels={},
            last_heartbeat_at=_utcnow(),
        )
        session.add(row)
        try:
            await session.commit()
            return CREATED
        except IntegrityError as exc:
            await session.rollback()
            if not _is_unique_violation(exc):
                raise
            # 并发注册：另一个事务已插入，下一轮按更新处理。
            continue

    logger.warning("register ignored: 并发冲突未收敛 node_key=%s", node_key)
    return IGNORED


# -------------------------------------------------------------------- heartbeat
async def handle_heartbeat(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_key: str,
    payload: dict,
    dedupe=None,
) -> str:
    """更新 metrics / last_heartbeat_at / status；未知节点不创建假节点。"""
    if not is_valid_node_key(node_key) or not isinstance(payload, dict):
        logger.warning("heartbeat ignored: 非法 node_key 或载荷")
        return IGNORED

    msg_id = _msg_id(payload)
    if msg_id is None:
        logger.warning("heartbeat ignored: 缺少合法 msg_id node_key=%s", node_key)
        return IGNORED

    payload_key = payload.get("node_key")
    if payload_key is not None and payload_key != node_key:
        return IGNORED

    if not await _claim(dedupe, tenant_id, node_key, msg_id):
        return DUPLICATE

    row = await _get_node(session, tenant_id, node_key)
    if row is None:
        logger.info("heartbeat ignored: 未知节点 node_key=%s", node_key)
        return IGNORED

    # 原始指标照抄；缺失字段不补，避免伪造测量。
    row.metrics = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}
    row.last_heartbeat_at = _utcnow()
    if not _is_disabled(row):
        row.status = "online"
    await session.commit()
    return UPDATED


# ----------------------------------------------------------------------- status
async def handle_status(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_key: str,
    payload: str | dict,
    dedupe=None,
) -> str:
    """LWT ``offline`` / 上线 ``online``；无 msg_id，是唯一允许不去重的消息。"""
    if not is_valid_node_key(node_key):
        return IGNORED

    if isinstance(payload, dict):
        value = payload.get("status")
    elif isinstance(payload, str):
        value = payload.strip()
    else:
        value = None

    value = value.lower() if isinstance(value, str) else None
    if value not in {"online", "offline"}:
        logger.warning("status ignored: 非法状态值 node_key=%s", node_key)
        return IGNORED

    row = await _get_node(session, tenant_id, node_key)
    if row is None:
        logger.info("status ignored: 未知节点 node_key=%s", node_key)
        return IGNORED

    if _is_disabled(row):
        if row.status != "disabled":
            row.status = "disabled"
            await session.commit()
            return UPDATED
        return IGNORED

    row.status = value
    await session.commit()
    return UPDATED


# ------------------------------------------------------------------------ event
async def handle_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_key: str,
    payload: dict,
    dedupe=None,
) -> str:
    """视觉事件入库；``dedup_key`` 唯一约束与消息去重双保险。"""
    if not is_valid_node_key(node_key) or not isinstance(payload, dict):
        logger.warning("event ignored: 非法 node_key 或载荷")
        return IGNORED

    msg_id = _msg_id(payload)
    if msg_id is None:
        logger.warning("event ignored: 缺少合法 msg_id node_key=%s", node_key)
        return IGNORED

    event_type = _optional_str(payload.get("event_type"), max_len=32)
    occurred_at = _parse_datetime(payload.get("occurred_at"))
    if event_type is None or occurred_at is None:
        logger.warning("event ignored: 缺少 event_type/occurred_at node_key=%s", node_key)
        return IGNORED

    dedup_source = payload.get("dedup_key")
    if dedup_source is not None and not isinstance(dedup_source, str):
        return IGNORED
    dedup_key = (dedup_source or "").strip() or f"{node_key}:{msg_id}"
    if len(dedup_key) > 128:
        # 列宽 128；超长键用稳定摘要，保证同一原始键仍然幂等。
        dedup_key = "sha256:" + hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()

    if not await _claim(dedupe, tenant_id, node_key, msg_id):
        return DUPLICATE

    node = await _get_node(session, tenant_id, node_key)
    node_id = node.id if node is not None else None

    device_id: int | None = None
    raw_device_id = payload.get("device_id")
    if raw_device_id is not None:
        if isinstance(raw_device_id, bool) or not isinstance(raw_device_id, int):
            return IGNORED
        if node_id is None:
            return IGNORED
        device = await session.scalar(
            select(Device.id).where(
                Device.id == raw_device_id,
                Device.tenant_id == tenant_id,
                Device.node_id == node_id,
            )
        )
        if device is None:
            logger.warning(
                "event ignored: device 不属于该租户/节点 node_key=%s device_id=%s",
                node_key,
                raw_device_id,
            )
            return IGNORED
        device_id = raw_device_id

    bbox = payload.get("bbox")
    if bbox is not None and not isinstance(bbox, dict):
        return IGNORED

    row = VisionEvent(
        tenant_id=tenant_id,
        node_id=node_id,
        device_id=device_id,
        event_type=event_type,
        severity=_optional_str(payload.get("severity"), max_len=16),
        occurred_at=occurred_at,
        confidence=_to_decimal(payload.get("confidence")),
        track_id=_optional_str(payload.get("track_id"), max_len=64),
        snapshot_path=_optional_str(payload.get("snapshot_path"), max_len=512),
        bbox=bbox,
        model_version=_optional_str(payload.get("model_version"), max_len=32),
        payload=payload.get("payload"),
        dedup_key=dedup_key,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _is_unique_violation(exc):
            return DUPLICATE
        raise
    return CREATED


# --------------------------------------------------------------------- task ack
async def handle_task_ack(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_key: str,
    payload: dict,
    dedupe=None,
) -> str:
    """ack 只做去重与日志；工作流状态接线留给 P6。"""
    if not is_valid_node_key(node_key) or not isinstance(payload, dict):
        return IGNORED

    msg_id = _msg_id(payload)
    if msg_id is None:
        return IGNORED

    if not await _claim(dedupe, tenant_id, node_key, msg_id):
        return DUPLICATE

    status = _optional_str(payload.get("status"), max_len=32) or "unknown"
    task_id = _optional_str(payload.get("task_id"), max_len=64)
    logger.info(
        "task ack node_key=%s task_id=%s status=%s", node_key, task_id or "-", status
    )
    return ACKNOWLEDGED
