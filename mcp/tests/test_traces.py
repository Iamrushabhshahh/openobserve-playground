from __future__ import annotations

from conftest import load_fixture

from agent_obs_mcp import traces as tr


def tree() -> tr.TraceTree:
    return tr.build_tree(load_fixture("trace_spans.json"))


def test_tree_structure() -> None:
    t = tree()
    assert [r.span_id for r in t.roots] == ["s0"]
    assert [c.span_id for c in t.nodes["s0"].children] == ["l1", "t1", "t2"]
    assert [c.span_id for c in t.nodes["t1"].children] == ["b1", "e1"]


def test_self_time_clips_and_merges_children() -> None:
    t = tree()
    assert t.nodes["s0"].self_ms == 0
    assert t.nodes["t1"].self_ms == 0
    assert t.nodes["t2"].self_ms == 100
    assert t.nodes["e1"].self_ms == 3000


def test_self_time_counts_parallel_children_once() -> None:
    rows = [
        {"span_id": "p", "operation_name": "x", "start_time": 0, "end_time": 10_000_000},
        {"span_id": "a", "reference_parent_span_id": "p", "start_time": 0, "end_time": 6_000_000},
        {
            "span_id": "b",
            "reference_parent_span_id": "p",
            "start_time": 2_000_000,
            "end_time": 8_000_000,
        },
    ]
    assert tr.build_tree(rows).nodes["p"].self_ms == 2.0


def test_critical_path_follows_latest_finishing_child() -> None:
    t = tree()
    assert [n.span_id for n in tr.critical_path(t.main_root())] == ["s0", "t2", "l3"]
    assert tr.critical_path(None) == []


def test_totals_and_top_self_time() -> None:
    t = tree()
    assert tr.totals(t) == {
        "spans": 8,
        "llm_ms": 6500.0,
        "tool_execution_ms": 3000.0,
        "blocked_on_user_ms": 1000.0,
        "subagents": 1,
    }
    assert tr.top_self_time(t)[0].span_id == "e1"
    assert len(tr.top_self_time(t)) == 5


def test_orphans_become_roots_and_render() -> None:
    rows = [
        *load_fixture("trace_spans.json"),
        {
            "span_id": "x",
            "reference_parent_span_id": "gone",
            "operation_name": "claude_code.tool",
            "tool_name": "Read",
            "start_time": 1,
            "end_time": 2,
        },
    ]
    t = tr.build_tree(rows)
    assert {r.span_id for r in t.roots} == {"s0", "x"}
    lines = tr.render_tree(t, {"s0"})
    assert lines[0].startswith("- tool Read")
    assert any(line.startswith("- ★ interaction") for line in lines)
    assert any(line.startswith("    - tool.execution") for line in lines)


def test_fanout_flags_work_after_interaction_end() -> None:
    agents, end = tr.fanout(load_fixture("trace_spans.json"))
    by_id = {a.agent_id: a for a in agents}
    assert agents[0].agent_id == "main"
    assert by_id["a1"].llm_calls == 2
    assert by_id["a1"].tokens == 40
    assert by_id["main"].tokens == 1150
    assert tr.overrun_ms(by_id["a1"], end) == 500
    assert tr.overrun_ms(by_id["a1"], None) == 0
