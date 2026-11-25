from app.observability.redaction import SENSITIVE_KEY_RE, redact, redact_record


def test_masks_sensitive_keys_recursively():
    payload = {"user": {"email": "a@b.com", "password": "x"},
               "headers": {"Authorization": "Bearer abc"}}
    out = redact(payload, "REDACTED")
    assert out["user"]["email"] == "***"
    assert out["user"]["password"] == "***"
    assert out["headers"]["Authorization"] == "***"


def test_masks_patterns_inside_plain_strings():
    out = redact({"note": "key=sk-abcdef123456 phone=13800138000"}, "REDACTED")
    assert "sk-abcdef123456" not in out["note"]
    assert "13800138000" not in out["note"]


def test_full_mode_passthrough_and_metadata_mode_empty():
    payload = {"api_key": "sk-1", "text": "hello"}
    assert redact(payload, "FULL") == payload
    assert redact(payload, "METADATA") is None


def test_record_redaction_keeps_structure_and_drops_bodies_in_metadata():
    rec = {"kind": "trace.start", "input": {"api_key": "sk-1", "q": "hi"}}
    out = redact_record(rec, "METADATA")
    assert out["input"] is None
    assert out["kind"] == "trace.start"

    out2 = redact_record(rec, "REDACTED")
    assert out2["input"]["api_key"] == "***"
    assert out2["input"]["q"] == "hi"


def test_token_counts_ids_and_timestamps_are_never_masked():
    """计数字段/主键/时间戳是观测数据本身，脱敏损坏它们等于丢数据。"""
    rec = {"tenant_id": "00000000-0000-0000-0000-000000000001",
           "trace_id": "a" * 32, "span_id": "b" * 16,
           "start_time": "2026-11-25T00:00:00Z",
           "kind": "llm.close",
           "usage": {"prompt_tokens": 12, "completion_tokens": 8,
                     "total_tokens": 20, "cached_tokens": 0},
           "total_tokens": 20, "total_cost": None,
           "api_key": "sk-should-be-masked"}
    out = redact_record(rec, "REDACTED")
    assert out["tenant_id"] == rec["tenant_id"]
    assert out["trace_id"] == rec["trace_id"]
    assert out["span_id"] == rec["span_id"]
    assert out["start_time"] == rec["start_time"]
    assert out["usage"]["total_tokens"] == 20
    assert out["total_tokens"] == 20
    assert out["total_cost"] is None
    assert out["api_key"] == "***"
