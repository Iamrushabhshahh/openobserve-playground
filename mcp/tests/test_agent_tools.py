from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from conftest import NOW_S, TEST_ENV, FakeO2, load_fixture

from agent_obs_mcp import agent_tools as at
from agent_obs_mcp.client import O2Client
from agent_obs_mcp.config import load_settings
from agent_obs_mcp.deps import Deps
from agent_obs_mcp.server import build_server
from agent_obs_mcp.sqlguard import CONTENT_COLUMNS

pytestmark = pytest.mark.anyio

GATED = CONTENT_COLUMNS | at.GATED_COLUMNS
_SCHEMA_PATH = re.compile(r"/streams/([^/]+)/schema$")


def schema_deps(
    routes: list[tuple[str, list[dict[str, Any]]]],
    schemas: dict[tuple[str, str], list[str]],
    **env: str,
) -> tuple[Deps, FakeO2]:
    """Deps whose transport also answers stream-schema GETs (keyed by stream, type)."""
    fake = FakeO2(routes)

    def handler(request: httpx.Request) -> httpx.Response:
        match = _SCHEMA_PATH.search(request.url.path)
        if request.method == "GET" and match:
            key = (match.group(1), request.url.params.get("type", ""))
            if key not in schemas:
                return httpx.Response(404, json={"code": 404, "message": "stream not found"})
            fields = [{"name": n, "type": "Utf8"} for n in schemas[key]]
            return httpx.Response(200, json={"name": key[0], "schema": fields})
        return fake(request)

    settings = load_settings({**TEST_ENV, **env})
    client = O2Client(settings, httpx.MockTransport(handler))
    return Deps(settings=settings, client=client, clock=lambda: NOW_S), fake


def selected_columns(sql: str) -> set[str]:
    head = sql.split(" FROM ", 1)[0].removeprefix("SELECT ")
    return {c.strip() for c in head.split(",")}


def mentions_gated(sql: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", re.sub(r"'(?:[^']|'')*'", "''", sql)))
    return {w for w in words if w.lower() in GATED}


# detect_loops


def test_find_loops_on_synthetic_spans() -> None:
    suspects = at.rank_suspects(at.find_loops(load_fixture("agent_loop_spans.json"), 3))
    by = {(s.trace_id, s.pattern, s.detail): s for s in suspects}
    run = by[("t-loop", "repeated_call", "Bash:npm")]
    assert (run.count, run.failures, run.confidence) == (4, 3, "high")
    assert run.wasted_s == pytest.approx(15.0)
    chain = by[("t-loop", "failure_retry_chain", "Bash:npm")]
    assert chain.count == 3 and chain.wasted_s == pytest.approx(15.0)
    storm = by[("t-storm", "retry_storm", "failed llm_request spans")]
    assert storm.count == 3 and storm.session_id == "sess-b"
    assert by[("t-loop", "repeated_call", "Read")].confidence == "low"
    assert not any(s.trace_id == "t-ok" for s in suspects)
    assert suspects[-1].detail == "Read"


def test_failure_chain_reset_by_success() -> None:
    calls = [
        at.ToolCall(span_id=str(i), key="Bash:git", tool="Bash", start_ns=i, seconds=1, failed=f)
        for i, f in enumerate([True, False, True, True])
    ]
    [chain] = at.failure_chains("t", calls)
    assert chain.count == 2


def test_min_repeats_raises_threshold() -> None:
    suspects = at.find_loops(load_fixture("agent_loop_spans.json"), 5)
    assert [s.pattern for s in suspects] == ["failure_retry_chain"]


async def test_detect_loops_end_to_end_merges_storms(make_deps) -> None:
    bursts = [{"trace_id": "t-storm", "session_id": "sess-b", "n": 7}]
    deps, fake = make_deps(
        [
            ("HAVING COUNT(*) >= 3", bursts),
            ("operation_name IN", load_fixture("agent_loop_spans.json")),
        ]
    )
    out = await at.find_loop_suspects(deps, "24h", 3)
    storms = [s for s in out.data["suspects"] if s["pattern"] == "retry_storm"]
    assert len(storms) == 1 and storms[0]["count"] == 7
    assert out.data["suspects"][0]["confidence"] == "high"
    assert "Loop suspects, last 24h" in out.markdown and "t-loop" in out.markdown
    span_sql = next(s for s in fake.sqls() if "operation_name IN" in s)
    assert "file_path" not in span_sql and "bash_argv0" in span_sql
    assert "Same-file repeats need file_path" in out.markdown


async def test_detect_loops_same_target_only_with_content() -> None:
    rows = [
        {
            "trace_id": "t1",
            "span_id": f"s{i}",
            "operation_name": "claude_code.tool",
            "tool_name": "Read",
            "file_path": "/repo/a.py",
            "start_time": i * 10**9,
            "duration": 1_000_000,
        }
        for i in range(4)
    ]
    rows.insert(2, {**rows[0], "span_id": "x", "tool_name": "Edit", "file_path": ""})
    schemas = {("claude_code", "traces"): [*at.VERIFIED_COLUMNS["traces"], "file_path"]}
    deps, fake = schema_deps([("operation_name IN", rows)], schemas, AGENT_OBS_ALLOW_CONTENT="1")
    out = await at.find_loop_suspects(deps, "24h", 3)
    assert "file_path" in fake.sqls()[0]
    same = [s for s in out.data["suspects"] if s["pattern"] == "same_target"]
    assert same and same[0]["count"] == 4 and same[0]["detail"] == "Read /repo/a.py"


async def test_detect_loops_empty(make_deps) -> None:
    deps, _ = make_deps()
    out = await at.find_loop_suspects(deps, "24h")
    assert "No data" in out.markdown


# compare_sessions


def test_ratio_flag() -> None:
    assert at.ratio_flag(2.0, 1.0) == 2.0
    assert at.ratio_flag(1.5, 1.0) is None
    assert at.ratio_flag(0, 3) == float("inf")
    assert at.ratio_flag(0, 0) is None
    assert at.ratio_flag(None, 4) is None


async def test_compare_sessions_flags_2x(make_deps) -> None:
    logs = [
        {
            "session_id": "sa",
            "cost_usd": "4.0",
            "llm_calls": 30,
            "tool_results": 40,
            "tool_failures": 0,
            "api_errors": 0,
            "input_tokens": 100,
            "cache_read_tokens": 900,
            "cache_creation_tokens": 0,
        },
        {
            "session_id": "sb",
            "cost_usd": "1.0",
            "llm_calls": 20,
            "tool_results": 35,
            "tool_failures": 3,
            "api_errors": 0,
            "input_tokens": 500,
            "cache_read_tokens": 500,
            "cache_creation_tokens": 0,
        },
    ]
    models = [
        {"session_id": "sa", "model": "claude-opus", "n": 30},
        {"session_id": "sb", "model": "claude-sonnet", "n": 20},
    ]
    spans = [
        {"session_id": "sa", "tool_calls": 40, "wait_us": 120e6, "p95_tool_us": 2_000_000},
        {"session_id": "sb", "tool_calls": 35, "wait_us": 600e6, "p95_tool_us": 1_500_000},
    ]
    deps, fake = make_deps(
        [
            ("GROUP BY session_id, model", models),
            ("approx_percentile_cont", spans),
            ("TRY_CAST(cost_usd", logs),
        ]
    )
    out = await at.compare_two_sessions(deps, "sa", "sb", "7d")
    flags = {f["metric"]: f for f in out.data["flags"]}
    assert flags["cost_usd"] == {"metric": "cost_usd", "ratio": 4.0, "higher": "sa"}
    assert flags["permission_wait_min"]["higher"] == "sb"
    assert flags["tool_failures"]["ratio"] is None
    assert "llm_calls" not in flags and "p95_tool_ms" not in flags
    assert out.data["a"]["cache_hit_pct"] == 90.0 and out.data["b"]["cache_hit_pct"] == 50.0
    assert out.data["a"]["models"] == ["claude-opus"]
    assert "⚠ 4.0×" in out.markdown and "| models | claude-opus | claude-sonnet |" in out.markdown
    assert all("FILTER" not in sql for sql in fake.sqls())


async def test_compare_sessions_validation(make_deps) -> None:
    deps, fake = make_deps()
    with pytest.raises(ValueError):
        await at.compare_two_sessions(deps, "same", "same")
    with pytest.raises(ValueError):
        await at.compare_two_sessions(deps, "a' OR 1=1", "b")
    assert fake.requests == []


async def test_compare_sessions_none_found(make_deps) -> None:
    deps, _ = make_deps()
    out = await at.compare_two_sessions(deps, "sa", "sb")
    assert "Neither session" in out.markdown


# audit_trail


def audit_routes() -> list[tuple[str, list[dict[str, Any]]]]:
    return [
        ("ORDER BY start_time ASC", load_fixture("agent_audit_spans.json")),
        ("ORDER BY _timestamp ASC", load_fixture("agent_audit_logs.json")),
    ]


async def test_audit_trail_redacts_content(make_deps) -> None:
    deps, fake = make_deps(audit_routes())
    out = await at.session_audit_trail(deps, "sess-1", include_commands=True)
    for sql in fake.sqls():
        assert mentions_gated(sql) == set(), sql
    text = out.markdown + json.dumps(out.data)
    for col in ("full_command", "tool_input", "user_prompt", "file_path"):
        assert f'"{col}"' not in json.dumps(out.data)
    assert "Content REDACTED" in out.markdown and "include_commands was ignored" in out.markdown
    assert out.data["content_redacted"] is True
    assert "command" not in out.data["entries"][0]
    assert "curl" in text


async def test_audit_trail_entries_are_chronological(make_deps) -> None:
    deps, _ = make_deps(audit_routes())
    out = await at.session_audit_trail(deps, "sess-1")
    entries = out.data["entries"]
    assert [e["time_us"] for e in entries] == sorted(e["time_us"] for e in entries)
    events = [e["event"] for e in entries]
    assert "tool result" not in events
    assert events[0] == "user prompt (redacted)"
    calls = [e for e in entries if e["event"] == "tool call"]
    assert calls[0]["command_class"] == "network/curl" and calls[0]["source"] == "user_temporary"
    assert calls[1]["outcome"] == "not run" and calls[1]["decision"] == "reject"
    assert calls[2]["outcome"] == "failed (Error:ENOENT)" and calls[2]["decision"] == "no prompt"
    mode = next(e for e in entries if e["event"] == "permission mode change")
    assert mode["decision"] == "bypassPermissions"
    mcp = next(e for e in entries if e["event"] == "mcp connection")
    assert (mcp["tool"], mcp["outcome"]) == ("github", "connected")
    api = next(e for e in entries if e["event"] == "api error")
    assert api["outcome"] == "HTTP 529"
    assert out.data["counts"]["tool call"] == 3
    assert "- Window: last 30d" in out.markdown and "Entries: 9" in out.markdown


async def test_audit_trail_without_spans_keeps_tool_results(make_deps) -> None:
    deps, _ = make_deps([("ORDER BY _timestamp ASC", load_fixture("agent_audit_logs.json"))])
    out = await at.session_audit_trail(deps, "sess-1")
    assert out.data["counts"]["tool result"] == 1


async def test_audit_trail_commands_when_allowed() -> None:
    spans = load_fixture("agent_audit_spans.json")
    spans[0]["full_command"] = "curl https://example.com"
    schemas = {("claude_code", "traces"): [*at.VERIFIED_COLUMNS["traces"], "full_command"]}
    routes = [("ORDER BY start_time ASC", spans)]
    deps, fake = schema_deps(routes, schemas, AGENT_OBS_ALLOW_CONTENT="1")
    out = await at.session_audit_trail(deps, "sess-1", include_commands=True)
    span_sql = next(s for s in fake.sqls() if "ORDER BY start_time ASC" in s)
    assert "full_command" in selected_columns(span_sql)
    assert "Commands INCLUDED" in out.markdown
    assert out.data["entries"][0]["command"] == "curl https://example.com"


async def test_audit_trail_validates_session(make_deps) -> None:
    deps, fake = make_deps()
    with pytest.raises(ValueError):
        await at.session_audit_trail(deps, "x'; DROP")
    assert fake.requests == []


# risky_actions


def test_classify_command() -> None:
    assert at.classify_command("other", "sudo") == ("privilege escalation", 5)
    assert at.classify_command("package_manager", "npm")[1] == 2
    assert at.classify_command("vcs", "git")[1] == 1
    assert at.classify_command("other", "ls") is None
    assert at.classify_command("network", None) == ("network", 3)


async def test_risky_actions_ranking(make_deps) -> None:
    bash = [
        {"bash_command_class": "vcs", "bash_argv0": "git", "n": 40, "example": "t-git"},
        {"bash_command_class": "other", "bash_argv0": "sudo", "n": 1, "example": "t-sudo"},
        {"bash_command_class": "network", "bash_argv0": "curl", "n": 5, "example": "t-curl"},
        {"bash_command_class": "other", "bash_argv0": "ls", "n": 99, "example": "t-ls"},
        {"bash_command_class": "other", "bash_argv0": "kubectl", "n": 2, "example": "t-k8s"},
    ]
    decisions = [
        {
            "tool_name": "Bash",
            "decision": "reject",
            "source": "user_reject",
            "n": 4,
            "example": "t-r",
        },
        {
            "tool_name": "Edit",
            "decision": "reject",
            "source": "user_abort",
            "n": 2,
            "example": "t-a",
        },
    ]
    modes = [{"session_id": "s1", "mode": "bypassPermissions", "n": 1}]
    mcp = [
        {"server_name": "github", "status": "connected", "n": 3, "example": "s1"},
        {"server_name": "flaky", "status": "failed", "n": 2, "example": "s2"},
    ]
    deps, fake = make_deps(
        [
            ("tool_name = 'Bash'", bash),
            ("event_name = 'tool_decision'", decisions),
            ("permission_mode_changed", modes),
            ("mcp_server_connection", mcp),
        ]
    )
    out = await at.review_risky_actions(deps, "7d")
    findings = out.data["findings"]
    assert [f["severity"] for f in findings[:2]] == ["critical", "critical"]
    assert {findings[0]["item"], findings[1]["item"]} == {"sudo", "→ bypassPermissions"}
    assert findings[2]["item"] == "kubectl" and findings[2]["examples"] == ["t-k8s"]
    items = [f["item"] for f in findings]
    assert "ls" not in items
    assert items.index("curl") < items.index("git")
    assert [f["item"] for f in findings[-2:]] == ["git", "github"]
    abort = next(f for f in findings if f["category"] == "user abort")
    assert abort["count"] == 2
    assert next(f for f in findings if f["item"] == "flaky")["category"] == "mcp failure"
    for sql in fake.sqls():
        assert mentions_gated(sql) == set()
    assert "never command text" in out.markdown


async def test_risky_actions_nothing(make_deps) -> None:
    deps, _ = make_deps()
    out = await at.review_risky_actions(deps, "7d")
    assert "No data" in out.markdown


# agents_overview

CLAUDE_TOTALS = [
    {
        "sessions": 4,
        "model_calls": 120,
        "tool_calls": 300,
        "tool_failures": 5,
        "api_errors": 1,
        "input_tokens": 1000,
        "output_tokens": 2000,
        "cached_tokens": 50000,
        "cost_usd": "12.5",
    }
]


async def test_agents_overview_codex_absent(make_deps) -> None:
    deps, fake = make_deps([('FROM "claude_code"', CLAUDE_TOTALS)])
    out = await at.overview_agents(deps, "7d")
    assert out.data["codex_connected"] is False
    [claude] = out.data["agents"]
    assert claude["agent"] == "Claude Code" and claude["failures"] == 6
    assert claude["cost_usd"] == 12.5
    assert "[otel]" in out.markdown
    assert "http://o2.test:5080/api/default/v1/logs" in out.markdown
    assert '"stream-name" = "codex"' in out.markdown
    assert "s3cr3t" not in out.markdown and "me@example.com" not in out.markdown
    assert any('FROM "codex"' in s for s in fake.sqls())


async def test_agents_overview_codex_stream_missing() -> None:
    routes = [('FROM "claude_code"', CLAUDE_TOTALS)]
    deps, fake = schema_deps(routes, {})
    fake.routes.append(('FROM "codex"', []))
    out = await at.overview_agents(deps, "7d")
    assert out.data["codex_connected"] is False and "no Codex events" in out.data["codex_note"]


async def test_agents_overview_with_codex() -> None:
    codex_fields = [
        "_timestamp",
        "event_name",
        "conversation_id",
        "success",
        "input_token_count",
        "output_token_count",
        "cached_token_count",
        "arguments",
        "output",
    ]
    codex_row = [
        {
            "events": 50,
            "sessions": 2,
            "model_calls": 10,
            "tool_calls": 15,
            "tool_failures": 1,
            "api_errors": 0,
            "input_tokens": 9000,
            "output_tokens": 800,
            "cached_tokens": 4000,
            "cost_usd": None,
        }
    ]
    routes = [('FROM "codex"', codex_row), ('FROM "claude_code"', CLAUDE_TOTALS)]
    deps, fake = schema_deps(routes, {("codex", "logs"): codex_fields})
    out = await at.overview_agents(deps, "7d")
    assert out.data["codex_connected"] is True
    codex = out.data["agents"][1]
    assert (codex["agent"], codex["sessions"], codex["input_tokens"]) == ("Codex CLI", 2, 9000)
    assert codex["cost_usd"] is None
    sql = next(s for s in fake.sqls() if 'FROM "codex"' in s)
    assert "COUNT(DISTINCT conversation_id)" in sql and "'codex.tool_result'" in sql
    assert "NULL AS cost_usd" in sql and mentions_gated(sql) == set()
    assert "[otel]" not in out.markdown


# registration


async def test_tools_registered_read_only() -> None:
    server = build_server(load_settings(TEST_ENV))
    tools = {t.name: t for t in await server.list_tools()}
    for name in (
        "detect_loops",
        "compare_sessions",
        "audit_trail",
        "risky_actions",
        "agents_overview",
    ):
        assert tools[name].annotations.readOnlyHint is True


async def test_tool_error_is_clean() -> None:
    server = build_server(load_settings(TEST_ENV))
    result = await server.call_tool("compare_sessions", {"session_a": "x", "session_b": "x"})
    content = result[0] if isinstance(result, tuple) else result
    text = content[0].text if isinstance(content, list) else content.content[0].text
    assert "must differ" in text
