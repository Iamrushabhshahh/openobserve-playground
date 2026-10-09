from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from conftest import load_fixture

from agent_obs_mcp.analytics import (
    ToolWait,
    aggregate_waits_by_tool,
    allow_rule_candidates,
)

IST = ZoneInfo("Asia/Kolkata")


def parents() -> dict[str, str]:
    return {r["span_id"]: r["tool_name"] for r in load_fixture("parent_tool_spans.json")}


def test_waits_grouped_by_own_or_parent_tool() -> None:
    tools = {
        t.tool: t for t in aggregate_waits_by_tool(load_fixture("blocked_spans.json"), parents())
    }
    assert set(tools) == {"Read", "Edit", "(unknown)"}
    read = tools["Read"]
    assert (read.waits, read.manual_accepts, read.total_us, read.max_us) == (5, 5, 10e6, 4e6)
    edit = tools["Edit"]
    assert (edit.waits, edit.accepts, edit.rejects, edit.manual_accepts) == (2, 1, 1, 0)


def test_tools_sorted_by_total_wait() -> None:
    tools = aggregate_waits_by_tool(load_fixture("blocked_spans.json"), parents())
    assert [t.tool for t in tools] == ["Edit", "Read", "(unknown)"]


def test_allow_rule_candidates_threshold() -> None:
    tools = aggregate_waits_by_tool(load_fixture("blocked_spans.json"), parents())
    assert [c["tool"] for c in allow_rule_candidates(tools)] == ["Read"]


@pytest.mark.parametrize(
    ("manual", "waits", "expected"),
    [(5, 5, True), (4, 4, False), (19, 20, True), (18, 20, False)],
)
def test_allow_rule_boundaries(manual: int, waits: int, expected: bool) -> None:
    t = ToolWait("Grep", waits=waits, manual_accepts=manual, accepts=manual)
    assert bool(allow_rule_candidates([t])) is expected


def test_bash_suggestion_is_scoped() -> None:
    t = ToolWait("Bash", waits=9, manual_accepts=9, accepts=9)
    assert allow_rule_candidates([t])[0]["rule"].startswith("Bash(<command prefix>:*)")
