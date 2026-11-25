import re

# 注意排除 *_tokens 计数：token 既可能是凭据名，也可能是用量字段，
# 只有真正像凭据的键才脱敏
TOKEN_COUNT_KEY_RE = re.compile(
    r"^(total|prompt|completion|cached|input|output)?_?tokens?(_count)?$", re.I)
SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|token|password|secret|cookie|credential"
    r"|phone|id_card|email)", re.I)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
SK_RE = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")
PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")

# 这些字段只在 REDACTED/FULL 里出现内容，METADATA 一律置 null
BODY_FIELDS = ("input", "output", "request_messages", "rendered_messages",
               "input_arguments", "output_result", "raw_response",
               "structured_response", "final_context", "retrieved_documents",
               "reranked_documents", "variables", "events", "llm_calls",
               "tool_calls", "retrievals")


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key)) and not TOKEN_COUNT_KEY_RE.match(key)


def redact(value, mode: str):
    if mode in ("OFF", "METADATA"):
        return None
    if mode == "FULL":
        return value
    if isinstance(value, dict):
        return {k: ("***" if is_sensitive_key(k) else redact(v, mode))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, mode) for v in value]
    if isinstance(value, str):
        return _scrub(value)
    return value


def _scrub(text: str) -> str:
    text = BEARER_RE.sub("Bearer ***", text)
    text = SK_RE.sub("sk-***", text)
    text = PHONE_RE.sub("***", text)
    text = ID_CARD_RE.sub("***", text)
    return text


def redact_record(record: dict, mode: str) -> dict:
    """只在记录内容层面工作：kind/ids/tenant_id/时间戳/计数永远原样保留。"""
    if mode == "FULL":
        return record
    if mode == "METADATA":
        return {k: (None if k in BODY_FIELDS else v) for k, v in record.items()}
    return redact(record, "REDACTED")
