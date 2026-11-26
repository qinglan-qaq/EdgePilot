from __future__ import annotations

import time

from fakes import SlowEngine, install_fake_cv2, make_client, make_settings, write_video


def _wait_job(client, task_uid: str, statuses: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/v1/jobs/{task_uid}").json()
        if last.get("status") in statuses:
            return last
        time.sleep(0.01)
    raise AssertionError(f"timeout waiting for {statuses}, last={last}")


def test_health_without_token_reports_missing_weights(client):
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["model"]["status"] == "missing_weights"
    assert body["storage"]["writable"] is True
    assert body["queue"]["max_workers"] >= 1


def test_probe_maps_errors(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, max_file_bytes=16)
    install_fake_cv2(monkeypatch, frame_count=10)
    video = write_video(tmp_path / "big.mp4", size=100)
    with make_client(settings) as client:
        response = client.post("/v1/probe", json={"video_path": str(video.resolve())})
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_probe_rejects_relative_path(tmp_path, monkeypatch):
    install_fake_cv2(monkeypatch, frame_count=10)
    write_video(tmp_path / "clip.mp4")
    with make_client(make_settings(tmp_path)) as client:
        response = client.post("/v1/probe", json={"video_path": "clip.mp4"})
    assert response.status_code == 400
    assert response.json()["code"] == "video_missing"


def test_job_lifecycle_and_results(tmp_path):
    video = write_video(tmp_path / "clip.mp4")
    with make_client(make_settings(tmp_path)) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "task_uid": "task-0001",
                "video_path": str(video.resolve()),
                "filename": "clip.mp4",
                "params": {"conf_threshold": 0.3, "target_samples": 900},
            },
        )
        assert response.status_code == 202
        state = _wait_job(client, "task-0001", {"completed"})
        assert state["progress"] == 100
        assert state["result_available"] is True
        artifact = client.get("/v1/jobs/task-0001/results").json()
    assert artifact["schema_version"] == "vision-result/v1"
    assert artifact["event_id"] == "task-0001"
    assert artifact["summary"]["detection_count"] == 1
    assert artifact["frames"][0]["detections"][0]["class_name"] == "person"


def test_unknown_job_is_404(client):
    response = client.get("/v1/jobs/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "job_not_found"


def test_token_is_required_when_configured(tmp_path):
    settings = make_settings(tmp_path, token="secret-token")
    with make_client(settings) as client:
        assert client.get("/v1/models").status_code == 401
        ok = client.get("/v1/models", headers={"X-Vision-Token": "secret-token"})
        assert ok.status_code == 200
        assert ok.json()["models"][0]["license"] == "AGPL-3.0"


def test_cancel_running_job_via_api(tmp_path):
    video = write_video(tmp_path / "clip.mp4")
    with make_client(make_settings(tmp_path), engine=SlowEngine()) as client:
        client.post(
            "/v1/jobs",
            json={"task_uid": "task-slow", "video_path": str(video.resolve())},
        )
        _wait_job(client, "task-slow", {"running"})
        response = client.post("/v1/jobs/task-slow/cancel")
        assert response.status_code == 200
        state = _wait_job(client, "task-slow", {"cancelled"})
        assert state["result_available"] is False
        assert client.get("/v1/jobs/task-slow/results").status_code == 409


def test_reload_models_reports_missing_weights(tmp_path):
    with make_client(make_settings(tmp_path), engine=SlowEngine()) as client:
        body = client.post("/v1/models/reload").json()
    assert body["model"]["load_status"] == "missing_weights"


def test_models_endpoint_lists_license(client):
    body = client.get("/v1/models").json()
    assert body["models"][0]["license"] == "AGPL-3.0"


def test_job_rejects_dot_segment_task_uid(tmp_path, monkeypatch):
    """task_uid 的字符集允许点号，但 "."/".." 会成为路径段，必须在契约层拒绝。"""
    install_fake_cv2(monkeypatch, frame_count=10)
    video = write_video(tmp_path / "clip.mp4")
    with make_client(make_settings(tmp_path)) as client:
        for task_uid in (".", ".."):
            response = client.post(
                "/v1/jobs",
                json={"task_uid": task_uid, "video_path": str(video.resolve())},
            )
            assert response.status_code == 422, (task_uid, response.text)
