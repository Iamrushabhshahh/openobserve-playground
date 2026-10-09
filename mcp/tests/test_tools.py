from __future__ import annotations

import pytest
from conftest import NOW_S, TEST_ENV, load_fixture

from agent_obs_mcp import claude_tools as ct
from agent_obs_mcp import sql_tool
from agent_obs_mcp.client import O2Error
from agent_obs_mcp.render import MAX_OUTPUT_CHARS
from agent_obs_mcp.server import build_server

pytestmark = pytest.mark.anyio

SESSION_ROWS = [
    {
        "session_id": "sess-1",
        "cost_usd": "1.5",
        "llm_calls": 12,
        "tool_results": 30,
        "tool_failures": 2,
        "api_errors": 0,
        "first_seen": 1_759_900_000_000_000,
        "last_seen": 1_759_903_600_000_000,
        "span_us": 3_600_000_000,
    },
]


async def test_list_sessions_request_shape(make_deps) -> None:
    deps, fake = make_deps(
        [
            ("GROUP BY session_id, model", [{"session_id": "sess-1", "model": "opus", "n": 12}]),
            ("GROUP BY session_id", SESSION_ROWS),
        ]
    )
    out = await ct.list_sessions(deps, "7d", 5, "llm_calls")
    first = fake.requests[0]
    assert first["type"] == "logs"
    assert first["headers"]["authorization"].startswith("Basic ")
    assert first["end_time"] == int(NOW_S * 1_000_000)
    assert first["end_time"] - first["start_time"] == 7 * 86_400 * 1_000_000
    assert first["size"] == 5
    assert (
        'FROM "claude_code"' in first["sql"] and "ORDER BY llm_calls DESC LIMIT 5" in first["sql"]
    )
    assert out.data["sessions"][0]["models"] == ["opus"]
    assert out.data["sessions"][0]["active_span_min"] == 60.0
    assert "| sess-1 | 1.5 | 12 |" in out.markdown


async def test_list_sessions_rejects_bad_sort(make_deps) -> None:
    deps, _ = make_deps()
    with pytest.raises(ValueError):
        await ct.list_sessions(deps, sort="name; DROP")


async def test_get_trace_tree_end_to_end(make_deps) -> None:
    deps, fake = make_deps([("WHERE trace_id = 'abc123'", load_fixture("trace_spans.json"))])
    out = await ct.get_trace_tree(deps, "abc123")
    assert fake.requests[0]["type"] == "traces"
    assert [n["span_id"] for n in out.data["critical_path"]] == ["s0", "t2", "l3"]
    assert out.data["totals"]["subagents"] == 1
    assert "★ tool Task" in out.markdown


async def test_get_trace_tree_validates_id(make_deps) -> None:
    deps, fake = make_deps()
    with pytest.raises(ValueError):
        await ct.get_trace_tree(deps, "x' OR '1'='1")
    assert fake.requests == []


async def test_permission_report_joins_parent_tools(make_deps) -> None:
    summary = [
        {
            "source": "user_temporary",
            "decision": "accept",
            "waits": 5,
            "total_us": 10e6,
            "max_us": 4e6,
        }
    ]
    deps, fake = make_deps(
        [
            ("GROUP BY source, decision", summary),
            ("span_id IN (", load_fixture("parent_tool_spans.json")),
            ("SELECT span_id, reference_parent_span_id", load_fixture("blocked_spans.json")),
        ]
    )
    out = await ct.permission_wait_report(deps, "30d")
    assert any("'p1'" in sql and "claude_code.tool'" in sql for sql in fake.sqls())
    assert [s["tool"] for s in out.data["suggestions"]] == ["Read"]
    assert out.data["by_source"][0]["avg_s"] == 2.0
    assert "`Read`" in out.markdown


async def test_subagent_fanout_flags_overrun(make_deps) -> None:
    deps, _ = make_deps([("trace_id = 'abc123'", load_fixture("trace_spans.json"))])
    out = await ct.subagent_fanout(deps, "abc123")
    assert out.data["continued_after_end"] is True
    assert "Work continued after the interaction ended" in out.markdown


async def test_cache_efficiency_handles_utf8_numbers(make_deps) -> None:
    rows = [
        {
            "model": "opus",
            "calls": "4",
            "input_tokens": "100",
            "cache_read_tokens": "800",
            "cache_creation_tokens": "100",
        }
    ]
    deps, _ = make_deps([("GROUP BY model", rows)])
    out = await ct.cache_efficiency(deps)
    assert out.data["models"][0]["hit_rate_pct"] == 80.0


async def test_run_readonly_sql_strips_content_from_star(make_deps) -> None:
    deps, fake = make_deps([("FROM claude_code", [{"model": "m", "user_prompt": "secret"}])])
    out = await sql_tool.run_readonly_sql(deps, "SELECT * FROM claude_code", "logs", "1h", 1000)
    assert fake.requests[0]["sql"].endswith("LIMIT 500")
    assert fake.requests[0]["size"] == 500
    assert out.data["rows"] == [{"model": "m"}]
    assert "secret" not in out.markdown


async def test_run_readonly_sql_blocks_before_any_request(make_deps) -> None:
    deps, fake = make_deps()
    with pytest.raises(ValueError):
        await sql_tool.run_readonly_sql(deps, "SELECT * FROM users")
    assert fake.requests == []


async def test_auth_failure_never_echoes_credentials(make_deps) -> None:
    deps, _ = make_deps(status=401)
    with pytest.raises(O2Error) as err:
        await ct.cost_report(deps)
    message = str(err.value)
    assert "HTTP 401" in message
    assert TEST_ENV["O2_PASSWORD"] not in message and "Basic" not in message


async def test_server_call_returns_markdown_structured_and_errors(make_deps) -> None:
    import httpx

    from agent_obs_mcp.config import load_settings

    _, fake = make_deps([("GROUP BY k", [{"k": "opus", "cost_usd": 2.5, "calls": 3}])])
    server = build_server(load_settings(TEST_ENV), httpx.MockTransport(fake), clock=lambda: NOW_S)
    ok = await server.call_tool("cost_report", {"window": "7d", "by": "model"})
    assert "Total: $2.50" in ok.content[0].text
    assert ok.structuredContent["total_usd"] == 2.5
    bad = await server.call_tool("run_readonly_sql", {"sql": "DROP TABLE claude_code"})
    assert bad.isError and "Error:" in bad.content[0].text


async def test_output_is_capped(make_deps) -> None:
    rows = [{"session_id": f"s{i:04d}" + "x" * 60, "cost_usd": i} for i in range(100)]
    deps, _ = make_deps([("GROUP BY session_id", rows)])
    out = (await ct.list_sessions(deps, limit=100)).capped()
    assert len(out.markdown) <= MAX_OUTPUT_CHARS
    assert "truncated" in out.markdown and out.data["truncated"] is True


async def test_resources_and_prompts_registered() -> None:
    from agent_obs_mcp.config import load_settings

    server = build_server(load_settings(TEST_ENV))
    uris = {str(r.uri) for r in await server.list_resources()}
    assert uris == {"agentobs://schema/claude-code", "agentobs://about"}
    tools = await server.list_tools()
    writes = {"create_alert", "create_dashboard"}
    names = {t.name for t in tools}
    assert len(tools) == len(names) >= 40 and writes <= names
    assert all(t.annotations.readOnlyHint == (t.name not in writes) for t in tools)
    prompt = await server.get_prompt("investigate_slow_session", {"session_id": "abc'; x"})
    assert (
        "abc" in prompt.messages[0].content.text and "'; x" not in prompt.messages[0].content.text
    )
