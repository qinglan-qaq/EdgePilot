"""Prometheus 指标定义与文本输出。

标签取值必须保持低基数：HTTP 的 path 使用路由模板（如 ``/api/v1/audit-logs``），
绝不使用带参数或查询串的原始 URL。
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

http_request_total = Counter("http_request_total", "HTTP 请求总数", ["method", "path", "status"])
http_request_duration = Histogram("http_request_duration_seconds", "HTTP 请求耗时", ["method", "path"])
audit_write_total = Counter("audit_write_total", "审计写入总数", ["action", "result"])


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
