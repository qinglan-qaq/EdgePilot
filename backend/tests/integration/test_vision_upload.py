"""Task 11 集成测试：上传校验、指纹幂等、并发插入、派发与重试。

真实 PostgreSQL（conftest 共享事务 / 并发用例使用独立连接）+ 测试内 Fake
VisionClient 与临时存储根。只伪造外部推理服务，不伪造任何推理结果。

上传事务约定（plan Task 11）：
临时文件 → probe（含 model_version）→ 有效步长与指纹 → 查重 → 原子移动
→ 插入（唯一约束兜底）→ 派发；派发失败只改任务状态，上传仍返回 200。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.datastructures import Headers, UploadFile

from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.vision import VisionTask
from app.services import vision_task_service as task_service_module
from app.services.vision_client import VisionServiceError
from app.services.vision_task_service import VisionTaskService, fingerprint
from tests.conftest import OTHER_TENANT, TENANT, auth_headers

# 偏移 4 起为 ftyp，满足 MP4 魔数校验；填充内容对 Fake probe 无意义。
MP4_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"
UPLOAD_URL = "/api/v1/vision/tasks"


# ---------------------------------------------------------------- helpers


def _valid_mp4(size: int = 2048) -> bytes:
    """生成魔数合法且内容随机的 mp4；随机填充保证不同用例的 SHA-256 不碰撞。"""
    filler = (uuid.uuid4().bytes * (size // 16 + 2))[: max(size, 64)]
    return MP4_HEADER + filler


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _probe_payload(*, frame_count: int = 250, fps: float = 25.0) -> dict:
    duration_ms = int(round(frame_count / fps * 1000)) if fps else 10_000
    return {
        "filename": "gate.mp4",
        "size_bytes": 2048,
        "width": 1280,
        "height": 720,
        "fps": fps,
        "frame_count": frame_count,
        "duration_ms": duration_ms,
        "duration_known": True,
        "max_video_seconds": 900,
        "max_samples": 2000,
        "target_samples": 900,
        "model_name": "yolo11n",
        "model_version": "yolo11n-0123456789ab",
        "weights_sha256": "b" * 64,
    }


class FakeVisionClient:
    """VisionClient 的进程内替身；只用于上传/派发协议，不产生推理结果。"""

    def __init__(self) -> None:
        self.ok = True
        self.dispatch_ok = True
        self.probe_data = _probe_payload()
        self.probe_gate: asyncio.Barrier | None = None
        self.probe_calls: list[str] = []
        self.job_calls: list[dict] = []
        self.cancel_calls: list[str] = []

    async def probe(self, video_path: str) -> dict:
        self.probe_calls.append(str(video_path))
        if self.probe_gate is not None:
            # 超时避免实现异常时把测试永久挂起。
            await asyncio.wait_for(self.probe_gate.wait(), timeout=10)
        if not self.ok:
            raise VisionServiceError("推理服务不可达: ConnectError")
        return dict(self.probe_data)

    async def create_job(
        self,
        task_uid: str,
        video_path: str,
        filename: str,
        video_sha256: str,
        conf_threshold: float,
        target_samples: int,
        frame_stride: int | None,
    ) -> dict:
        self.job_calls.append(
            {
                "task_uid": task_uid,
                "video_path": str(video_path),
                "filename": filename,
                "video_sha256": video_sha256,
                "conf_threshold": float(conf_threshold),
                "target_samples": int(target_samples),
                "frame_stride": frame_stride,
            }
        )
        if not self.ok or not self.dispatch_ok:
            raise VisionServiceError("推理服务不可达: ConnectError")
        return {"task_uid": task_uid, "status": "pending", "progress": 0}

    async def get_job(self, task_uid: str) -> dict:
        if not self.ok:
            raise VisionServiceError("推理服务不可达: ConnectError")
        return {"task_uid": task_uid, "status": "pending", "progress": 0}

    async def cancel_job(self, task_uid: str) -> dict:
        self.cancel_calls.append(task_uid)
        return {"task_uid": task_uid, "status": "cancelled"}

    async def health(self) -> dict:
        return {"status": "ok" if self.ok else "down"}

    async def models(self) -> dict:
        return {"models": []}


async def _upload(client, token, data: bytes, *, filename: str = "gate.mp4",
                  content_type: str = "video/mp4", **form) -> object:
    return await client.post(
        UPLOAD_URL,
        headers=auth_headers(token),
        files={"file": (filename, data, content_type)},
        data={key: str(value) for key, value in form.items()} or None,
    )


async def _task_row(session, uid: str) -> VisionTask | None:
    return await session.scalar(
        select(VisionTask).where(
            VisionTask.tenant_id == TENANT, VisionTask.task_uid == uid
        )
    )


async def _task_count(session, tenant_id=TENANT) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(VisionTask).where(
                VisionTask.tenant_id == tenant_id
            )
        )
        or 0
    )


async def _seed_failed_task(session, vision_root: Path, **overrides) -> VisionTask:
    uid = str(uuid.uuid4())
    values = {
        "tenant_id": TENANT,
        "task_uid": uid,
        "dedup_key": hashlib.sha256(uid.encode("utf-8")).hexdigest(),
        "status": "failed",
        "source_type": "upload",
        "video_filename": "gate.mp4",
        "video_path": f"videos/{TENANT}/{uid}.mp4",
        "video_sha256": hashlib.sha256(uid.encode("utf-8")).hexdigest(),
        "file_size_bytes": 2048,
        "content_type": "video/mp4",
        "conf_threshold": 0.25,
        "frame_stride": 1,
        "target_samples": 900,
        "error_code": "vision_service_unavailable",
        "error_message": "推理服务不可达",
        "dispatch_attempts": 0,
        "created_by": 1,
    }
    values.update(overrides)
    task = VisionTask(**values)
    session.add(task)
    await session.flush()
    video = vision_root / task.video_path
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(_valid_mp4())
    return task


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def vision_root(tmp_path, monkeypatch):
    """把 VISION_STORAGE_DIR 指到临时目录，避免污染仓库 var/vision。"""
    monkeypatch.setattr(get_settings(), "vision_storage_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_vision(monkeypatch):
    fake = FakeVisionClient()
    # 同时替换服务模块内的名字与源类，覆盖“模块级 import”和“调用时 import”两种写法。
    monkeypatch.setattr(task_service_module, "VisionClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(
        "app.services.vision_client.VisionClient", lambda *args, **kwargs: fake
    )
    return fake


# ---------------------------------------------------------------- 上传校验


async def test_upload_persists_relative_path_and_dispatches_absolute_path(
    client, session, admin_token, fake_vision, vision_root
):
    data = _valid_mp4()
    response = await _upload(client, admin_token, data)
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    task = body["task"]
    assert body["deduplicated"] is False
    assert task["status"] in {"pending", "running"}

    row = await _task_row(session, task["task_uid"])
    assert row is not None
    assert row.video_sha256 == _sha256(data)
    assert row.file_size_bytes == len(data)
    # DB 只存相对 VISION_STORAGE_DIR 的路径。
    assert row.video_path == f"videos/{TENANT}/{row.task_uid}.mp4"
    assert not Path(row.video_path).is_absolute()
    stored = vision_root / row.video_path
    assert stored.is_file()
    # 派发必须使用 resolve() 后的绝对路径。
    assert fake_vision.job_calls, "成功上传后必须派发"
    call = fake_vision.job_calls[0]
    assert call["task_uid"] == row.task_uid
    assert Path(call["video_path"]).is_absolute()
    assert Path(call["video_path"]) == stored.resolve()
    assert call["video_sha256"] == row.video_sha256
    assert row.dispatch_attempts == 1


async def test_upload_enforces_200mb_limit_without_persisting_task(
    client, session, admin_token, fake_vision, vision_root, monkeypatch
):
    settings = get_settings()
    assert settings.vision_max_upload_mb == 200  # 设计默认上限

    # 真正传 200MB 会拖慢测试；把上限缩到 1MB，验证同一条流式计数/中止路径。
    monkeypatch.setattr(settings, "vision_max_upload_mb", 1)
    response = await _upload(client, admin_token, _valid_mp4(1024 * 1024 + 64))

    assert response.status_code == 413, response.text
    assert response.json()["data"]["reason"] == "file_too_large"
    assert await _task_count(session) == 0
    assert fake_vision.probe_calls == []
    assert list(vision_root.rglob(".tmp-*")) == [], "超限后必须删除临时文件"


async def test_upload_rejects_bad_magic_unsupported_suffix_and_empty_file(
    client, session, admin_token, fake_vision, vision_root
):
    cases = [
        (b"definitely-not-a-video" * 8, "gate.mp4"),
        (_valid_mp4(), "gate.txt"),
        (b"", "gate.mp4"),
    ]
    for data, filename in cases:
        response = await _upload(client, admin_token, data, filename=filename)
        assert response.status_code == 400, (filename, response.text)
        assert response.json()["data"]["reason"] == "invalid_file_type"

    assert await _task_count(session) == 0
    assert fake_vision.probe_calls == []
    assert list(vision_root.rglob(".tmp-*")) == []


async def test_upload_rejects_invalid_zones_with_400_and_no_task(
    client, session, admin_token, fake_vision, vision_root
):
    """spec 错误表：区域非法 → 400 invalid_zones，且在任何写出/探测前拒绝。"""
    cases = [
        "{not-json",
        json.dumps(
            [{"zone_id": "gate", "polygon": [[0.0, 0.0], [1.0, 0.0], [2.0, 1.0]]}]
        ),
        json.dumps([{"zone_id": "gate", "polygon": [[0.0, 0.0], [1.0, 0.0]]}]),
        json.dumps(
            [
                {"zone_id": "gate", "polygon": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]},
                {"zone_id": "gate", "polygon": [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0]]},
            ]
        ),
    ]
    for zones in cases:
        response = await _upload(client, admin_token, _valid_mp4(), zones=zones)
        assert response.status_code == 400, (zones, response.text)
        assert response.json()["data"]["reason"] == "invalid_zones"

    assert await _task_count(session) == 0
    assert fake_vision.probe_calls == []
    assert list(vision_root.rglob(".tmp-*")) == []


async def test_viewer_role_cannot_upload(
    client, session, viewer_token, fake_vision, vision_root
):
    """上传仅 admin/operator：viewer 403，且不产生任务。"""
    response = await _upload(client, viewer_token, _valid_mp4())
    assert response.status_code == 403, response.text
    assert await _task_count(session) == 0
    assert fake_vision.probe_calls == []


# ---------------------------------------------------------------- 指纹幂等 / 并发


async def test_duplicate_upload_returns_existing_task(
    client, session, admin_token, fake_vision, vision_root
):
    data = _valid_mp4()
    first = await _upload(client, admin_token, data)
    second = await _upload(client, admin_token, data)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_body = first.json()["data"]
    second_body = second.json()["data"]
    assert first_body["deduplicated"] is False
    assert second_body["deduplicated"] is True
    assert first_body["task"]["task_uid"] == second_body["task"]["task_uid"]
    assert await _task_count(session) == 1
    assert len(fake_vision.job_calls) == 1, "指纹命中不得重复派发"


async def test_concurrent_same_file_creates_one_task(fake_vision, vision_root):
    """两个独立连接同时插入同一指纹：唯一约束兜底，只留一个任务/一份文件。

    conftest 的共享 session 不能安全并发，因此这里用独立 engine 做真实竞争；
    测试结束显式删除已提交的行（该用例不走外层回滚事务）。
    """
    settings = get_settings()
    data = _valid_mp4(4096)
    digest = _sha256(data)
    engine = create_async_engine(settings.test_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user = SimpleNamespace(
        id=1,
        tenant_id=TENANT,
        email="admin@edgepilot.local",
        display_name="Admin",
        role="admin",
        status="active",
    )
    fake_vision.probe_gate = asyncio.Barrier(2)

    async def upload_once():
        token = set_tenant_id(TENANT)
        try:
            async with factory() as db:
                upload = UploadFile(
                    file=io.BytesIO(data),
                    filename="gate.mp4",
                    headers=Headers({"content-type": "video/mp4"}),
                )
                return await VisionTaskService(db).create_upload(
                    upload,
                    conf_threshold=0.25,
                    frame_stride=None,
                    target_samples=900,
                    zones=None,
                    user=user,
                    ip="127.0.0.1",
                    user_agent="pytest",
                )
        finally:
            reset_tenant_id(token)

    try:
        results = await asyncio.gather(upload_once(), upload_once())
        uids = {item.task.task_uid for item in results}
        assert len(uids) == 1
        assert {item.deduplicated for item in results} == {False, True}

        async with factory() as db:
            total = await db.scalar(
                select(func.count()).select_from(VisionTask).where(
                    VisionTask.tenant_id == TENANT,
                    VisionTask.video_sha256 == digest,
                )
            )
        assert total == 1

        tenant_videos = vision_root / "videos" / str(TENANT)
        assert len(list(tenant_videos.glob("*.mp4"))) == 1, "失败方必须删除自己的文件"
        assert list(tenant_videos.glob(".tmp-*")) == []
    finally:
        async with factory() as db:
            await db.execute(
                delete(VisionTask).where(
                    VisionTask.tenant_id == TENANT,
                    VisionTask.video_sha256 == digest,
                )
            )
            await db.commit()
        await engine.dispose()


# ---------------------------------------------------------------- vision 不可达 / 重试


async def test_vision_unavailable_creates_failed_task_and_keeps_video(
    client, session, admin_token, fake_vision, vision_root
):
    fake_vision.ok = False
    response = await _upload(client, admin_token, _valid_mp4())

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    task = body["task"]
    assert body["deduplicated"] is False
    assert task["status"] == "failed"
    assert task["error_code"] == "vision_service_unavailable"
    assert task["error_message"]
    row = await _task_row(session, task["task_uid"])
    assert row is not None
    assert row.dispatch_attempts == 0
    assert (vision_root / row.video_path).is_file(), "失败任务必须保留视频供 retry"
    assert fake_vision.job_calls == []


async def test_dispatch_failure_returns_200_with_failed_task(
    client, session, admin_token, fake_vision, vision_root
):
    """probe 成功但派发失败：上传仍 200，任务 failed 且保留视频供 retry。"""
    fake_vision.dispatch_ok = False
    response = await _upload(client, admin_token, _valid_mp4())

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    task = body["task"]
    assert body["deduplicated"] is False
    assert task["status"] == "failed"
    assert task["error_code"] == "vision_service_unavailable"
    assert task["error_message"]

    row = await _task_row(session, task["task_uid"])
    assert row is not None
    assert row.dispatch_attempts == 1
    assert (vision_root / row.video_path).is_file(), "派发失败也必须保留视频"
    assert len(fake_vision.job_calls) == 1


async def test_vision_down_duplicate_upload_is_deduplicated(
    client, session, admin_token, fake_vision, vision_root
):
    fake_vision.ok = False
    data = _valid_mp4()
    first = await _upload(client, admin_token, data)
    second = await _upload(client, admin_token, data)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"]["deduplicated"] is False
    assert second.json()["data"]["deduplicated"] is True
    assert (
        first.json()["data"]["task"]["task_uid"]
        == second.json()["data"]["task"]["task_uid"]
    )
    assert await _task_count(session) == 1
    assert len(fake_vision.job_calls) == 0


async def test_probe_failure_fingerprint_uses_empty_model_version(
    client, session, admin_token, fake_vision, vision_root
):
    fake_vision.ok = False
    data = _valid_mp4()
    response = await _upload(client, admin_token, data)
    assert response.status_code == 200, response.text

    row = await _task_row(session, response.json()["data"]["task"]["task_uid"])
    assert row is not None
    assert row.status == "failed"
    assert row.model_version in (None, "")
    # probe 失败时没有真实 model_version，指纹必须使用 ""；步长回退到请求值/1。
    assert row.frame_stride == 1
    expected = fingerprint(
        TENANT,
        _sha256(data),
        get_settings().vision_model_key,
        "",
        0.25,
        900,
        row.frame_stride,
        None,
    )
    assert row.dedup_key == expected
    assert (
        fingerprint(
            TENANT,
            _sha256(data),
            get_settings().vision_model_key,
            "yolo11n-0123456789ab",
            0.25,
            900,
            row.frame_stride,
            None,
        )
        != expected
    )

    duplicate = await _upload(client, admin_token, data)
    assert duplicate.json()["data"]["deduplicated"] is True
    assert duplicate.json()["data"]["task"]["task_uid"] == row.task_uid


async def test_retry_reuses_failed_task_after_vision_recovers(
    client, session, admin_token, fake_vision, vision_root
):
    fake_vision.ok = False
    data = _valid_mp4()
    first = await _upload(client, admin_token, data)
    assert first.status_code == 200, first.text
    uid = first.json()["data"]["task"]["task_uid"]
    before = await _task_row(session, uid)
    assert before is not None and before.dispatch_attempts == 0

    fake_vision.ok = True
    retried = await client.post(
        f"{UPLOAD_URL}/{uid}/retry", headers=auth_headers(admin_token)
    )
    assert retried.status_code == 200, retried.text
    task = retried.json()["data"]["task"]
    assert task["task_uid"] == uid
    assert task["dispatch_attempts"] == 1
    assert task["status"] in {"pending", "running"}
    assert task["error_code"] is None
    assert not task["error_message"]

    assert len(fake_vision.job_calls) == 1, "重试必须重新派发同一任务"
    call = fake_vision.job_calls[0]
    assert call["task_uid"] == uid
    assert Path(call["video_path"]).is_absolute()
    assert await _task_count(session) == 1, "重试不得创建新任务"


async def test_retry_limit_exceeded_returns_409(
    client, session, admin_token, fake_vision, vision_root
):
    max_retries = get_settings().vision_max_retries
    task = await _seed_failed_task(
        session, vision_root, dispatch_attempts=max_retries
    )
    response = await client.post(
        f"{UPLOAD_URL}/{task.task_uid}/retry", headers=auth_headers(admin_token)
    )

    assert response.status_code == 409, response.text
    assert response.json()["data"]["reason"] == "retry_limit_exceeded"
    assert fake_vision.job_calls == []
    await session.refresh(task)
    assert task.dispatch_attempts == max_retries


async def test_retry_rejects_non_retryable_status(
    client, session, admin_token, fake_vision, vision_root
):
    response = await _upload(client, admin_token, _valid_mp4())
    assert response.status_code == 200, response.text
    uid = response.json()["data"]["task"]["task_uid"]

    retried = await client.post(
        f"{UPLOAD_URL}/{uid}/retry", headers=auth_headers(admin_token)
    )
    assert retried.status_code == 409, retried.text
    assert retried.json()["data"]["reason"] == "task_not_retryable"


# ---------------------------------------------------------------- 跨租户 / 有效步长


async def test_upload_fingerprint_and_retry_are_tenant_isolated(
    client, session, admin_token, other_tenant_token, fake_vision, vision_root
):
    data = _valid_mp4()
    mine = await _upload(client, admin_token, data)
    other = await _upload(client, other_tenant_token, data)

    assert mine.status_code == 200, mine.text
    assert other.status_code == 200, other.text
    mine_body = mine.json()["data"]
    other_body = other.json()["data"]
    # 指纹包含 tenant_id：同文件跨租户是新任务。
    assert mine_body["deduplicated"] is False
    assert other_body["deduplicated"] is False
    assert mine_body["task"]["task_uid"] != other_body["task"]["task_uid"]

    rows = (
        await session.scalars(
            select(VisionTask).where(VisionTask.video_sha256 == _sha256(data))
        )
    ).all()
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {TENANT, OTHER_TENANT}
    assert len({row.dedup_key for row in rows}) == 2

    # 另一租户看不到、也不能重试我的任务。
    cross = await client.post(
        f"{UPLOAD_URL}/{mine_body['task']['task_uid']}/retry",
        headers=auth_headers(other_tenant_token),
    )
    assert cross.status_code == 404, cross.text
    assert cross.json()["data"]["reason"] == "task_not_found"


async def test_effective_stride_uses_probe_frame_count_and_request_floor(
    client, session, admin_token, fake_vision, vision_root
):
    fake_vision.probe_data = _probe_payload(frame_count=5000)
    data = _valid_mp4()

    # estimated=5000, target_samples=100 → ceil(5000/100)=50，请求值 2 被抬高。
    first = await _upload(
        client, admin_token, data, frame_stride=2, target_samples=100
    )
    assert first.status_code == 200, first.text
    first_task = first.json()["data"]["task"]
    assert math.ceil(5000 / 100) == 50
    assert first_task["frame_stride"] == 50

    # 请求值 80 大于自动步长时保留 80，并因指纹参数不同创建新任务。
    second = await _upload(
        client, admin_token, data, frame_stride=80, target_samples=100
    )
    assert second.status_code == 200, second.text
    second_task = second.json()["data"]["task"]
    assert second_task["frame_stride"] == 80
    assert second_task["task_uid"] != first_task["task_uid"]
    assert second.json()["data"]["deduplicated"] is False
    assert await _task_count(session) == 2


# -------------------------------------------------- 时长校验与区域指纹


async def test_probe_video_too_long_rejected_with_422(
    client, session, admin_token, fake_vision, vision_root
):
    """时长超限属于请求级非法：422 拒绝，不落任务、不派发、不留临时文件。"""

    async def too_long(_path: str) -> dict:
        raise VisionServiceError(
            "视频时长 1200.0s 超过 900s 上限", status=422, code="video_too_long"
        )

    fake_vision.probe = too_long  # type: ignore[assignment]
    data = _valid_mp4()

    response = await _upload(client, admin_token, data)
    assert response.status_code == 422, response.text
    assert response.json()["data"]["reason"] == "video_too_long"
    assert await _task_count(session) == 0
    assert fake_vision.job_calls == []
    assert list(vision_root.rglob(".tmp-*")) == [], "拒绝后必须删除临时文件"


def test_fingerprint_canonicalizes_zone_json_and_separates_geometry():
    zones = [
        {"zone_id": "gate", "name": "大门", "polygon": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]},
        {"zone_id": "yard", "polygon": [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0]]},
    ]
    # 仅 dict 键顺序不同（JSON 序列化差异）不得改变指纹。
    reordered_keys = [
        {"polygon": zones[0]["polygon"], "name": zones[0]["name"], "zone_id": zones[0]["zone_id"]},
        {"polygon": zones[1]["polygon"], "zone_id": zones[1]["zone_id"]},
    ]
    changed = [
        zones[0],
        {"zone_id": "yard", "polygon": [[0.0, 0.0], [0.0, 0.5], [1.0, 1.0]]},
    ]

    base = fingerprint(
        TENANT, "a" * 64, "yolo11n", "yolo11n-abcdef123456", 0.25, 900, 1, zones
    )
    assert base == fingerprint(
        TENANT, "a" * 64, "yolo11n", "yolo11n-abcdef123456", 0.25, 900, 1, reordered_keys
    )
    assert base != fingerprint(
        TENANT, "a" * 64, "yolo11n", "yolo11n-abcdef123456", 0.25, 900, 1, changed
    )
    # 有区域与无区域必须是不同指纹（否则“加区域”会被误判为重复上传）。
    assert base != fingerprint(
        TENANT, "a" * 64, "yolo11n", "yolo11n-abcdef123456", 0.25, 900, 1, None
    )
