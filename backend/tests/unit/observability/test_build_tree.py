from app.observability.services.trace_service import build_tree


def span(span_id, parent, start="2026-11-25T10:00:00", **kw):
    return {"span_id": span_id, "parent_span_id": parent, "name": span_id,
            "start_time": start, "span_type": kw.get("span_type", "agent"), **kw}


def test_parent_child_assembly():
    tree = build_tree([span("a", None), span("b", "a"), span("c", "a")])
    assert len(tree) == 1
    assert [n["span_id"] for n in tree[0]["children"]] == ["b", "c"]


def test_orphan_becomes_root_with_flag():
    tree = build_tree([span("a", None), span("x", "missing-parent")])
    assert {n["span_id"] for n in tree} == {"a", "x"}
    orphan = next(n for n in tree if n["span_id"] == "x")
    assert orphan["orphan"] is True


def test_parallel_children_sorted_by_start_time():
    tree = build_tree([
        span("root", None),
        span("late", "root", "2026-11-25T10:00:05"),
        span("early", "root", "2026-11-25T10:00:01"),
    ])
    assert [c["span_id"] for c in tree[0]["children"]] == ["early", "late"]
