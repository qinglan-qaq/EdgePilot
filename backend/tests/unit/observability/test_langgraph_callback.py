"""LangGraph 观测回调的行为测试。

使用真实 ``Tracer`` + 内存 capturing collector，不触达 Redis/Postgres/网络。
合成的 3 节点图会被真实执行，验证 callback 只产出 1 个 workflow 根 span
和 3 个 node span。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TypedDict

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.callback import LangGraphCallback, langgraph_callback
from app.observability.tracer import Tracer

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class CapturingCollector:
    """把 Tracer 入队的记录原样保存在内存里。"""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.degraded = False

    def put_nowait(self, record: dict) -> bool:
        self.records.append(record)
        return True


def _make_tracer() -> tuple[Tracer, CapturingCollector]:
    collector = CapturingCollector()
    tracer = Tracer(
        enabled=True,
        sample_rate=1.0,
        capture_mode="FULL",
        collector=collector,
    )
    return tracer, collector


@contextmanager
def _tenant():
    token = set_tenant_id(TENANT)
    try:
        yield
    finally:
        reset_tenant_id(token)


class _State(TypedDict, total=False):
    value: int


def _alpha(state: _State) -> dict:
    # 节点内部再跑一个 Runnable：LangGraph 会把 langgraph_node 元数据传下去，
    # callback 必须把它识别为同一节点的内部链，不能重复建 node.alpha。
    RunnableLambda(lambda value: value + 0).invoke(state["value"])
    return {"value": state["value"] + 1}


def _beta(state: _State) -> dict:
    return {"value": state["value"] + 1}


def _gamma(state: _State) -> dict:
    return {"value": state["value"] + 1}


def _build_graph():
    graph = StateGraph(_State)
    graph.add_node("alpha", _alpha)
    graph.add_node("beta", _beta)
    graph.add_node("gamma", _gamma)
    graph.add_edge(START, "alpha")
    graph.add_edge("alpha", "beta")
    graph.add_edge("beta", "gamma")
    graph.add_edge("gamma", END)
    return graph.compile()


GRAPH = _build_graph()


def _span_opens(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("kind") == "span.open"]


def _span_closes(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("kind") == "span.close"]


def _node_closes(records: list[dict]) -> list[dict]:
    return [r for r in _span_closes(records) if str(r.get("name", "")).startswith("node.")]


def _events(records: list[dict]) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for record in _span_closes(records):
        for event in record.get("events") or []:
            events.append((event.get("event_type"), event.get("event_data") or {}))
    return events


def test_three_node_graph_emits_one_workflow_and_three_node_spans():
    tracer, collector = _make_tracer()
    callback = LangGraphCallback(tracer=tracer)

    with _tenant():
        result = GRAPH.invoke({"value": 1}, config={"callbacks": [callback]})

    assert result == {"value": 4}
    records = collector.records

    trace_starts = [r for r in records if r.get("kind") == "trace.start"]
    assert len(trace_starts) == 1
    assert trace_starts[0]["source"] == "langgraph"

    opens = _span_opens(records)
    assert len(opens) == 1
    root = opens[0]
    assert root["span_type"] == "workflow"
    assert root["parent_span_id"] is None
    assert str(root["name"]).startswith("workflow.")
    assert trace_starts[0]["root_span_id"] == root["span_id"]

    nodes = _node_closes(records)
    assert len(nodes) == 3
    assert {r["name"] for r in nodes} == {"node.alpha", "node.beta", "node.gamma"}
    assert [r["name"] for r in nodes] == ["node.alpha", "node.beta", "node.gamma"]
    for node in nodes:
        assert node["span_type"] == "workflow"
        assert node["parent_span_id"] == root["span_id"]
        assert node["status"] == "succeeded"
        assert node["node_id"] == node["name"].removeprefix("node.")
        assert node["metadata"]["instrumented_by"] == "langgraph"

    # 根 + 3 个节点各占一个 span id；节点内部 Runnable 没有多建 span。
    all_span_ids = {root["span_id"]} | {r["span_id"] for r in nodes}
    assert len(all_span_ids) == 4


def test_existing_active_trace_reuses_its_root_span():
    tracer, collector = _make_tracer()
    callback = LangGraphCallback(tracer=tracer)
    root_run = uuid.uuid4()
    node_run = uuid.uuid4()

    with _tenant():
        trace = tracer.start_trace("workflow.outer", source="workflow_run")
        try:
            callback.on_chain_start(None, {"value": 0}, run_id=root_run, metadata={})
            callback.on_chain_start(
                None, {"value": 0}, run_id=node_run,
                parent_run_id=root_run, metadata={"langgraph_node": "alpha"},
            )
            callback.on_chain_end({"value": 1}, run_id=node_run)
            callback.on_chain_end({"value": 1}, run_id=root_run)
        finally:
            trace.close()

    records = collector.records
    # 不新开 trace、不新开根 span。
    assert len([r for r in records if r.get("kind") == "trace.start"]) == 1
    opens = _span_opens(records)
    assert len(opens) == 1
    assert opens[0]["name"] == "workflow.outer"
    assert opens[0]["span_id"] == trace.root_span_id

    nodes = _node_closes(records)
    assert len(nodes) == 1
    assert nodes[0]["name"] == "node.alpha"
    assert nodes[0]["parent_span_id"] == trace.root_span_id


def test_interrupt_and_resume_add_exactly_two_events_to_root_span():
    tracer, collector = _make_tracer()
    callback = langgraph_callback(tracer=tracer)
    root_run = uuid.uuid4()

    with _tenant():
        callback.on_chain_start(None, {"value": 1}, run_id=root_run, metadata={})
        callback.on_interrupt({"reason": "approval_required"})
        callback.on_resume({"approved": True})
        callback.on_chain_end({"value": 1}, run_id=root_run)

    records = collector.records
    events = _events(records)
    assert [event[0] for event in events] == ["interrupt", "resume"]
    assert len(events) == 2
    assert events[0][1] == {"reason": "approval_required"}
    assert events[1][1] == {"approved": True}

    root_open = _span_opens(records)[0]
    event_records = [r for r in _span_closes(records) if r.get("events")]
    assert len(event_records) == 1
    assert event_records[0]["span_id"] == root_open["span_id"]


def test_nested_chain_without_langgraph_node_is_ignored():
    tracer, collector = _make_tracer()
    callback = LangGraphCallback(tracer=tracer)
    root_run = uuid.uuid4()
    nested_run = uuid.uuid4()
    deep_run = uuid.uuid4()

    with _tenant():
        callback.on_chain_start(None, {}, run_id=root_run, metadata={})
        callback.on_chain_start(
            None, {}, run_id=nested_run, parent_run_id=root_run, metadata={}
        )
        callback.on_chain_start(
            None, {}, run_id=deep_run,
            parent_run_id=nested_run, metadata={"checkpoint_ns": "x"},
        )
        callback.on_chain_end({}, run_id=deep_run)
        callback.on_chain_end({}, run_id=nested_run)
        callback.on_chain_end({}, run_id=root_run)

    records = collector.records
    assert _node_closes(records) == []
    # 只有根 span.open；嵌套链不建 span，也没有事件需要 span.close。
    assert len(_span_opens(records)) == 1
    assert _span_closes(records) == []
    assert len([r for r in records if r.get("kind") == "trace.close"]) == 1


def test_llm_and_tool_spans_parent_to_node_and_reuse_business_span():
    tracer, collector = _make_tracer()
    callback = LangGraphCallback(tracer=tracer)
    root_run = uuid.uuid4()
    node_run = uuid.uuid4()
    llm_run = uuid.uuid4()
    tool_run = uuid.uuid4()
    reuse_run = uuid.uuid4()

    with _tenant():
        callback.on_chain_start(None, {}, run_id=root_run, metadata={})
        callback.on_chain_start(
            None, {}, run_id=node_run,
            parent_run_id=root_run, metadata={"langgraph_node": "alpha"},
        )

        callback.on_chat_model_start(
            {"name": "ChatOpenAI", "kwargs": {"model_name": "gpt-test"}},
            [["hello"]],
            run_id=llm_run,
            parent_run_id=node_run,
            metadata={},
        )
        callback.on_llm_end(
            SimpleNamespace(
                llm_output={"token_usage": {"total_tokens": 3}},
                generations=[],
            ),
            run_id=llm_run,
        )

        callback.on_tool_start(
            {"name": "read_temp"}, "{}", run_id=tool_run,
            parent_run_id=node_run, metadata={},
        )
        callback.on_tool_end("ok", run_id=tool_run)

        # 业务侧已经开了同类型 span：callback 必须复用它，不新建、不代收口。
        business_span = tracer.start_span("llm.business", "llm")
        before = len(collector.records)
        callback.on_llm_start(
            {"name": "BusinessModel"}, ["hi"], run_id=reuse_run,
            parent_run_id=node_run, metadata={},
        )
        callback.on_llm_end(None, run_id=reuse_run)
        after_reuse = len(collector.records)
        business_span.finish()

        callback.on_chain_end({}, run_id=node_run)
        callback.on_chain_end({}, run_id=root_run)

    assert after_reuse == before, "复用的业务 span 不应产生额外观测记录"
    records = collector.records
    node_close = _node_closes(records)[0]

    llm_closes = [
        r for r in _span_closes(records) if r["span_type"] == "llm"
    ]
    assert len(llm_closes) == 2
    created_llm = [
        r for r in llm_closes if r["metadata"].get("instrumented_by") == "langgraph"
    ]
    assert len(created_llm) == 1
    created = created_llm[0]
    assert created["name"] == "llm.gpt-test"
    assert created["parent_span_id"] == node_close["span_id"]
    assert created["input"] == {"model_name": "gpt-test", "prompt_count": 1}
    assert created["status"] == "succeeded"
    assert created["output"] == {"token_usage": {"total_tokens": 3},
                                 "generation_batches": 0}
    assert business_span.span_id in {r["span_id"] for r in llm_closes}
    assert not [
        r for r in _span_closes(records) if r["name"] == "llm.BusinessModel"
    ]

    tool_closes = [
        r for r in _span_closes(records) if r["span_type"] == "tool"
    ]
    assert len(tool_closes) == 1
    tool = tool_closes[0]
    assert tool["name"] == "tool.read_temp"
    assert tool["parent_span_id"] == node_close["span_id"]
    assert tool["tool_id"] == "read_temp"
    assert tool["metadata"]["instrumented_by"] == "langgraph"
    assert tool["input"] == {"tool_name": "read_temp", "input": "{}"}
    assert tool["output"] == "ok"


def test_callback_failures_never_escape_into_the_graph():
    class ExplodingTracer:
        enabled = True
        sample_rate = 1.0
        capture_mode = "FULL"

        def start_trace(self, *args, **kwargs):
            raise RuntimeError("trace down")

        def start_span(self, *args, **kwargs):
            raise RuntimeError("span down")

        def emit(self, *args, **kwargs):
            raise RuntimeError("emit down")

    callback = LangGraphCallback(tracer=ExplodingTracer())

    with _tenant():
        result = GRAPH.invoke({"value": 1}, config={"callbacks": [callback]})
        # 直接调用也走同一套隔离逻辑。
        callback.on_interrupt({"x": 1})
        callback.on_resume({"x": 1})
        callback.on_llm_start(
            None, ["x"], run_id=uuid.uuid4(), metadata={}
        )
        callback.on_llm_error(
            RuntimeError("llm down"), run_id=uuid.uuid4()
        )
        callback.on_tool_start(
            None, "x", run_id=uuid.uuid4(), metadata={}
        )

    assert result == {"value": 4}
