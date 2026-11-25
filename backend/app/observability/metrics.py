from prometheus_client import Counter, Gauge, Histogram

trace_total = Counter("observability_trace_total", "Trace 总数", ["source", "status"])
span_total = Counter("observability_span_total", "Span 总数", ["span_type", "status"])
span_error_total = Counter("observability_span_error_total", "Span 错误数", ["span_type", "code"])
event_dropped = Counter("observability_event_dropped_total", "观测数据丢弃数", ["reason"])
write_latency = Histogram("observability_write_latency_seconds", "观测批量写入耗时")
llm_request_total = Counter("llm_request_total", "LLM 调用数", ["provider", "model"])
llm_token_total = Counter("llm_token_total", "LLM token 数", ["provider", "model", "kind"])
llm_request_duration_seconds = Histogram("llm_request_duration_seconds", "LLM 请求耗时",
                                         ["provider", "model"])
tool_call_total = Counter("tool_call_total", "工具调用数", ["tool_id", "status"])
tool_call_error_total = Counter("tool_call_error_total", "工具调用失败数", ["tool_id", "code"])

observability_collector_degraded = Gauge(
    "observability_collector_degraded", "采集队列是否处于降级模式（1=是，0=否）"
)
observability_degraded_transitions_total = Counter(
    "observability_degraded_transitions_total", "采集降级状态切换次数", ["state"]
)
