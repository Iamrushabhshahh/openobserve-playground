"""Claude Code telemetry tools (traces + logs in the claude_code stream)."""

from __future__ import annotations

from typing import Any

from . import traces as tr
from .analytics import aggregate_waits_by_tool, allow_rule_candidates
from .deps import Deps, chunks, clamp, in_list, require_id, sql_str
from .render import ToolOutput, empty, fmt_us_ts, heading, num, table, to_float
from .timewin import TimeRange, window_range

SESSION_SORT = {"cost": "cost_usd", "llm_calls": "llm_calls", "duration": "span_us"}
COST_KEYS = {
    "model": "COALESCE(model, '(unknown)')",
    "session": "COALESCE(session_id, '(unknown)')",
    "day": "histogram(_timestamp, '1 day')",
}
RAW_CAP = 10_000
API_ERROR_EVENTS = ("api_error", "api_retries_exhausted")
FAILED = "CAST(success AS VARCHAR) = 'false'"
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _tokens_sql() -> str:
    return (
        "SUM(TRY_CAST(input_tokens AS BIGINT)) AS input_tokens, "
        "SUM(TRY_CAST(output_tokens AS BIGINT)) AS output_tokens, "
        "SUM(TRY_CAST(cache_read_tokens AS BIGINT)) AS cache_read_tokens, "
        "SUM(TRY_CAST(cache_creation_tokens AS BIGINT)) AS cache_creation_tokens"
    )


async def list_sessions(
    deps: Deps, window: str = "7d", limit: int = 20, sort: str = "cost"
) -> ToolOutput:
    if sort not in SESSION_SORT:
        raise ValueError(f"sort must be one of {sorted(SESSION_SORT)}")
    limit = clamp(limit, 1, 100)
    rng = window_range(window, deps.clock())
    sql = (
        "SELECT session_id, "
        "SUM(CASE WHEN event_name = 'api_request' THEN TRY_CAST(cost_usd AS DOUBLE) ELSE 0 END)"
        " AS cost_usd, "
        "SUM(CASE WHEN event_name = 'api_request' THEN 1 ELSE 0 END) AS llm_calls, "
        "SUM(CASE WHEN event_name = 'tool_result' THEN 1 ELSE 0 END) AS tool_results, "
        f"SUM(CASE WHEN event_name = 'tool_result' AND {FAILED} THEN 1 ELSE 0 END)"
        " AS tool_failures, "
        f"SUM(CASE WHEN event_name IN ({in_list(API_ERROR_EVENTS)}) THEN 1 ELSE 0 END)"
        " AS api_errors, "
        "MIN(_timestamp) AS first_seen, MAX(_timestamp) AS last_seen, "
        "MAX(_timestamp) - MIN(_timestamp) AS span_us "
        f"FROM {deps.claude} WHERE session_id IS NOT NULL AND session_id <> '' "
        f"GROUP BY session_id ORDER BY {SESSION_SORT[sort]} DESC LIMIT {limit}"
    )
    rows = await deps.query(sql, "logs", rng, limit)
    if not rows:
        return empty("Sessions", f"No Claude Code log events in the last {window}.")
    models = await _session_models(deps, rng, [str(r["session_id"]) for r in rows])
    return _render_sessions(deps, rows, models, window, sort)


async def _session_models(deps: Deps, rng: TimeRange, ids: list[str]) -> dict[str, list[str]]:
    sql = (
        "SELECT session_id, model, COUNT(*) AS n "
        f"FROM {deps.claude} WHERE event_name = 'api_request' AND session_id IN ({in_list(ids)}) "
        "GROUP BY session_id, model ORDER BY n DESC LIMIT 1000"
    )
    out: dict[str, list[str]] = {}
    for row in await deps.query(sql, "logs", rng, 1000):
        if row.get("model"):
            out.setdefault(str(row["session_id"]), []).append(str(row["model"]))
    return out


def _render_sessions(
    deps: Deps, rows: list[dict], models: dict[str, list[str]], window: str, sort: str
) -> ToolOutput:
    tz = deps.settings.tz
    sessions = [
        {
            "session_id": str(r["session_id"]),
            "cost_usd": round(num(r.get("cost_usd")), 4),
            "llm_calls": int(num(r.get("llm_calls"))),
            "tool_results": int(num(r.get("tool_results"))),
            "tool_failures": int(num(r.get("tool_failures"))),
            "api_errors": int(num(r.get("api_errors"))),
            "models": models.get(str(r["session_id"]), []),
            "first_seen": fmt_us_ts(r.get("first_seen"), tz),
            "last_seen": fmt_us_ts(r.get("last_seen"), tz),
            "active_span_min": round(num(r.get("span_us")) / 60_000_000, 1),
        }
        for r in rows
    ]
    body = table(
        [
            "session_id",
            "cost $",
            "LLM calls",
            "tool results",
            "tool fails",
            "API errs",
            "models",
            "first seen",
            "span min",
        ],
        [
            [
                s["session_id"],
                s["cost_usd"],
                s["llm_calls"],
                s["tool_results"],
                s["tool_failures"],
                s["api_errors"],
                ", ".join(s["models"]) or None,
                s["first_seen"],
                s["active_span_min"],
            ]
            for s in sessions
        ],
    )
    title = heading(f"Sessions, last {window}, by {sort}", f"times in {deps.settings.tz_name}")
    return ToolOutput(f"{title}\n\n{body}", {"window": window, "sort": sort, "sessions": sessions})


async def get_trace_tree(
    deps: Deps, trace_id: str, max_spans: int = 500, window: str = "30d"
) -> ToolOutput:
    require_id("trace_id", trace_id)
    max_spans = clamp(max_spans, 1, 2000)
    sql = (
        "SELECT span_id, reference_parent_span_id, operation_name, tool_name, model, agent_id, "
        f"start_time, end_time, duration FROM {deps.claude} "
        f"WHERE trace_id = {sql_str(trace_id)} ORDER BY start_time LIMIT {max_spans}"
    )
    rows = await deps.query(sql, "traces", window_range(window, deps.clock()), max_spans)
    if not rows:
        return empty(f"Trace {trace_id}", f"No spans found in the last {window}; widen `window`.")
    tree = tr.build_tree(rows)
    path = tr.critical_path(tree.main_root())
    return _render_tree(trace_id, tree, path, capped=len(rows) >= max_spans)


def _render_tree(
    trace_id: str, tree: tr.TraceTree, path: list[tr.SpanNode], capped: bool
) -> ToolOutput:
    totals = tr.totals(tree)
    top = tr.top_self_time(tree)
    parts = [
        heading(f"Trace {trace_id}", "durations in ms; ★ = critical path"),
        table(
            ["spans", "LLM ms (summed)", "tool exec ms", "blocked on user ms", "sub-agents"],
            [
                [
                    totals["spans"],
                    totals["llm_ms"],
                    totals["tool_execution_ms"],
                    totals["blocked_on_user_ms"],
                    totals["subagents"],
                ]
            ],
        ),
        "### Critical path\n" + " → ".join(f"{n.label()} ({n.duration_ms:,.0f})" for n in path),
        "### Top self time\n"
        + table(
            ["span", "self ms", "total ms"], [[n.label(), n.self_ms, n.duration_ms] for n in top]
        ),
        "### Tree\n" + "\n".join(tr.render_tree(tree, {n.span_id for n in path})),
    ]
    if capped:
        parts.insert(1, "_Span limit reached: the tree may be incomplete; raise `max_spans`._")
    data = {
        "trace_id": trace_id,
        "totals": totals,
        "critical_path": [_node_dict(n) for n in path],
        "top_self_time": [_node_dict(n) for n in top],
        "roots": len(tree.roots),
        "span_limit_reached": capped,
    }
    return ToolOutput("\n\n".join(parts), data)


def _node_dict(node: tr.SpanNode) -> dict[str, Any]:
    return {
        "span_id": node.span_id,
        "operation": node.operation,
        "detail": node.detail,
        "duration_ms": round(node.duration_ms, 1),
        "self_ms": round(node.self_ms, 1),
    }


async def permission_wait_report(deps: Deps, window: str = "30d") -> ToolOutput:
    rng = window_range(window, deps.clock())
    blocked = f"operation_name = {sql_str(tr.OP_BLOCKED)}"
    summary_sql = (
        "SELECT COALESCE(source, 'unknown') AS source, COALESCE(decision, 'unknown') AS decision, "
        "COUNT(*) AS waits, SUM(TRY_CAST(duration AS DOUBLE)) AS total_us, "
        "MAX(TRY_CAST(duration AS DOUBLE)) AS max_us "
        f"FROM {deps.claude} WHERE {blocked} GROUP BY source, decision ORDER BY total_us DESC"
    )
    summary = await deps.query(summary_sql, "traces", rng, 200)
    if not summary:
        return empty("Permission waits", f"No blocked_on_user spans in the last {window}.")
    raw_sql = (
        "SELECT span_id, reference_parent_span_id, tool_name, decision, source, duration "
        f"FROM {deps.claude} WHERE {blocked} LIMIT {RAW_CAP}"
    )
    raw = await deps.query(raw_sql, "traces", rng, RAW_CAP)
    parents = await resolve_parent_tools(deps, rng, raw)
    per_tool = aggregate_waits_by_tool(raw, parents)
    return _render_permissions(window, summary, per_tool, sampled=len(raw) >= RAW_CAP)


async def resolve_parent_tools(
    deps: Deps, rng: TimeRange, rows: list[dict[str, Any]]
) -> dict[str, str]:
    """tool_name of the parent claude_code.tool span, for child spans that lack their own."""
    missing = sorted(
        {
            str(r.get("reference_parent_span_id"))
            for r in rows
            if not r.get("tool_name") and r.get("reference_parent_span_id")
        }
    )
    out: dict[str, str] = {}
    for part in chunks(missing):
        sql = (
            f"SELECT span_id, tool_name FROM {deps.claude} "
            f"WHERE operation_name = {sql_str(tr.OP_TOOL)} AND span_id IN ({in_list(part)}) "
            f"LIMIT {len(part)}"
        )
        for row in await deps.query(sql, "traces", rng, len(part)):
            if row.get("tool_name"):
                out[str(row["span_id"])] = str(row["tool_name"])
    return out


def _render_permissions(
    window: str, summary: list[dict], per_tool: list, sampled: bool
) -> ToolOutput:
    by_source = [
        {
            "source": r.get("source"),
            "decision": r.get("decision"),
            "waits": int(num(r.get("waits"))),
            "total_min": round(num(r.get("total_us")) / 60_000_000, 1),
            "avg_s": round(num(r.get("total_us")) / max(1, num(r.get("waits"))) / 1e6, 1),
            "max_min": round(num(r.get("max_us")) / 60_000_000, 2),
        }
        for r in summary
    ]
    tools = [
        {
            "tool": t.tool,
            "waits": t.waits,
            "total_min": round(t.total_us / 60_000_000, 1),
            "avg_s": round(t.total_us / max(1, t.waits) / 1e6, 1),
            "accepts": t.accepts,
            "rejects": t.rejects,
            "manual_accepts": t.manual_accepts,
        }
        for t in per_tool
    ]
    candidates = allow_rule_candidates(per_tool)
    parts = [
        heading(f"Permission waits, last {window}"),
        table(
            ["source", "decision", "waits", "total min", "avg s", "max min"],
            [list(r.values()) for r in by_source],
        ),
        "### By tool\n"
        + table(
            ["tool", "waits", "total min", "avg s", "accept", "reject", "manual accept"],
            [list(t.values()) for t in tools[:25]],
        ),
        "### Candidate `permissions.allow` rules\n" + _render_candidates(candidates),
    ]
    if sampled:
        parts.insert(2, f"_Per-tool numbers use the first {RAW_CAP} waits; totals are exact._")
    data = {"window": window, "by_source": by_source, "by_tool": tools, "suggestions": candidates}
    return ToolOutput("\n\n".join(parts), data)


def _render_candidates(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "None: no tool was approved by hand ≥95% of the time with ≥5 approvals."
    lines = [
        f"- `{c['rule']}` — approved by hand {c['manual_accepts']}/{c['waits']} times, "
        f"{c['wait_minutes']} min waiting"
        for c in candidates
    ]
    return "\n".join(lines) + "\n\nReview each rule before adding it to `.claude/settings.json`."


async def cost_report(deps: Deps, window: str = "7d", by: str = "model") -> ToolOutput:
    if by not in COST_KEYS:
        raise ValueError(f"by must be one of {sorted(COST_KEYS)}")
    order = "k ASC" if by == "day" else "cost_usd DESC"
    sql = (
        f"SELECT {COST_KEYS[by]} AS k, SUM(TRY_CAST(cost_usd AS DOUBLE)) AS cost_usd, "
        f"COUNT(*) AS calls, {_tokens_sql()} "
        f"FROM {deps.claude} WHERE event_name = 'api_request' GROUP BY k ORDER BY {order} LIMIT 100"
    )
    rows = await deps.query(sql, "logs", window_range(window, deps.clock()), 100)
    if not rows:
        return empty("Cost", f"No api_request events in the last {window}.")
    items = [
        {
            by: str(r.get("k")),
            "cost_usd": round(num(r.get("cost_usd")), 4),
            "calls": int(num(r.get("calls"))),
            "input_tokens": int(num(r.get("input_tokens"))),
            "output_tokens": int(num(r.get("output_tokens"))),
            "cache_read_tokens": int(num(r.get("cache_read_tokens"))),
        }
        for r in rows
    ]
    total = round(sum(i["cost_usd"] for i in items), 4)
    note = "days are UTC" if by == "day" else ""
    body = table(
        [by, "cost $", "calls", "input tok", "output tok", "cache read tok"],
        [list(i.values()) for i in items],
    )
    md = f"{heading(f'Cost by {by}, last {window}', note)}\n\n**Total: ${total:,.2f}**\n\n{body}"
    return ToolOutput(md, {"window": window, "by": by, "total_usd": total, "rows": items})


async def cache_efficiency(deps: Deps, window: str = "7d") -> ToolOutput:
    sql = (
        f"SELECT COALESCE(model, '(unknown)') AS model, COUNT(*) AS calls, {_tokens_sql()} "
        f"FROM {deps.claude} WHERE event_name = 'api_request' GROUP BY model ORDER BY calls DESC "
        "LIMIT 50"
    )
    rows = await deps.query(sql, "logs", window_range(window, deps.clock()), 50)
    if not rows:
        return empty("Prompt cache efficiency", f"No api_request events in the last {window}.")
    items = [_cache_row(str(r.get("model")), r) for r in rows]
    overall = _cache_row("ALL", {k: sum(num(r.get(k)) for r in rows) for k in _TOKEN_KEYS})
    overall["calls"] = sum(i["calls"] for i in items)
    body = table(
        ["model", "calls", "hit rate %", "cache read", "cache write", "uncached input"],
        [
            [
                i["model"],
                i["calls"],
                i["hit_rate_pct"],
                i["cache_read_tokens"],
                i["cache_creation_tokens"],
                i["input_tokens"],
            ]
            for i in [*items, overall]
        ],
    )
    md = (
        f"{heading(f'Prompt cache efficiency, last {window}')}\n\n"
        "hit rate = cache_read / (cache_read + input + cache_creation)\n\n" + body
    )
    return ToolOutput(md, {"window": window, "models": items, "overall": overall})


def _cache_row(model: str, row: dict[str, Any]) -> dict[str, Any]:
    read, inp, write = (
        num(row.get(k)) for k in ("cache_read_tokens", "input_tokens", "cache_creation_tokens")
    )
    denom = read + inp + write
    return {
        "model": model,
        "calls": int(num(row.get("calls"))),
        "hit_rate_pct": round(100 * read / denom, 1) if denom else None,
        "cache_read_tokens": int(read),
        "cache_creation_tokens": int(write),
        "input_tokens": int(inp),
    }


async def slowest_tools(deps: Deps, window: str = "7d", top: int = 10) -> ToolOutput:
    top = clamp(top, 1, 50)
    dur = "TRY_CAST(duration AS DOUBLE)"
    sql = (
        "SELECT COALESCE(tool_name, '(unknown)') AS tool, COUNT(*) AS n, "
        f"approx_percentile_cont({dur}, 0.5) AS p50_us, "
        f"approx_percentile_cont({dur}, 0.95) AS p95_us, MAX({dur}) AS max_us, "
        f"SUM({dur}) AS total_us FROM {deps.claude} WHERE operation_name = {sql_str(tr.OP_TOOL)} "
        f"GROUP BY tool ORDER BY p95_us DESC LIMIT {top}"
    )
    rows = await deps.query(sql, "traces", window_range(window, deps.clock()), top)
    if not rows:
        return empty("Slowest tools", f"No tool spans in the last {window}.")
    items = [
        {
            "tool": str(r.get("tool")),
            "calls": int(num(r.get("n"))),
            "p50_ms": round(num(r.get("p50_us")) / 1000, 1),
            "p95_ms": round(num(r.get("p95_us")) / 1000, 1),
            "max_ms": round(num(r.get("max_us")) / 1000, 1),
            "total_s": round(num(r.get("total_us")) / 1e6, 1),
        }
        for r in rows
    ]
    body = table(
        ["tool", "calls", "p50 ms", "p95 ms", "max ms", "total s"],
        [list(i.values()) for i in items],
    )
    note = "tool span time includes waiting for permission; see permission_wait_report"
    md = f"{heading(f'Slowest tools by p95, last {window}', note)}\n\n{body}"
    return ToolOutput(md, {"window": window, "tools": items})


async def failures(deps: Deps, window: str = "7d") -> ToolOutput:
    rng = window_range(window, deps.clock())
    exec_sql = (
        "SELECT trace_id, reference_parent_span_id, tool_name, error_class "
        f"FROM {deps.claude} WHERE operation_name = {sql_str(tr.OP_EXEC)} AND {FAILED} "
        f"ORDER BY _timestamp DESC LIMIT {RAW_CAP}"
    )
    failed = await deps.query(exec_sql, "traces", rng, RAW_CAP)
    parents = await resolve_parent_tools(deps, rng, failed)
    api_sql = (
        "SELECT event_name, COALESCE(CAST(status_code AS VARCHAR), '(none)') AS status, "
        "COALESCE(model, '(unknown)') AS model, COUNT(*) AS n "
        f"FROM {deps.claude} WHERE event_name IN ({in_list(API_ERROR_EVENTS)}) "
        "GROUP BY event_name, COALESCE(CAST(status_code AS VARCHAR), '(none)'), "
        "COALESCE(model, '(unknown)') ORDER BY n DESC LIMIT 50"
    )
    api = await deps.query(api_sql, "logs", rng, 50)
    return _render_failures(window, group_tool_failures(failed, parents), api)


def group_tool_failures(rows: list[dict[str, Any]], parents: dict[str, str]) -> list[dict]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        tool = (
            str(row.get("tool_name") or "")
            or parents.get(str(row.get("reference_parent_span_id") or ""), "")
            or "(unknown)"
        )
        err = str(row.get("error_class") or "(none)")
        g = groups.setdefault(
            (tool, err),
            {"tool": tool, "error_class": err, "count": 0, "example_trace_id": row.get("trace_id")},
        )
        g["count"] += 1
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)


def _render_failures(window: str, tools: list[dict], api: list[dict]) -> ToolOutput:
    api_rows = [
        {
            "event": r.get("event_name"),
            "status_code": r.get("status"),
            "model": r.get("model"),
            "count": int(num(r.get("n"))),
        }
        for r in api
    ]
    tool_md = (
        table(
            ["tool", "error_class", "count", "latest trace_id"],
            [list(t.values()) for t in tools[:30]],
        )
        if tools
        else "No failed tool executions."
    )
    api_md = (
        table(["event", "status", "model", "count"], [list(r.values()) for r in api_rows])
        if api_rows
        else "No API errors."
    )
    md = (
        f"{heading(f'Failures, last {window}')}\n\n### Tool executions\n{tool_md}"
        f"\n\n### API errors\n{api_md}"
    )
    return ToolOutput(md, {"window": window, "tool_failures": tools, "api_errors": api_rows})


async def subagent_fanout(deps: Deps, trace_id: str, window: str = "30d") -> ToolOutput:
    require_id("trace_id", trace_id)
    sql = (
        "SELECT span_id, operation_name, agent_id, start_time, end_time, "
        "duration, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens "
        f"FROM {deps.claude} WHERE trace_id = {sql_str(trace_id)} LIMIT 5000"
    )
    rows = await deps.query(sql, "traces", window_range(window, deps.clock()), 5000)
    if not rows:
        return empty(f"Sub-agent fan-out for {trace_id}", f"No spans in the last {window}.")
    agents, interaction_end = tr.fanout(rows)
    return _render_fanout(deps, trace_id, agents, interaction_end)


def _render_fanout(
    deps: Deps, trace_id: str, agents: list[tr.AgentStats], interaction_end: int | None
) -> ToolOutput:
    tz = deps.settings.tz
    items = []
    for a in agents:
        over = tr.overrun_ms(a, interaction_end)
        items.append(
            {
                "agent_id": a.agent_id,
                "llm_calls": a.llm_calls,
                "tokens": a.tokens,
                "llm_s": round(a.llm_ms / 1000, 1),
                "first": fmt_us_ts((a.first_ns or 0) / 1000, tz) if a.first_ns else None,
                "last": fmt_us_ts((a.last_ns or 0) / 1000, tz) if a.last_ns else None,
                "after_interaction_s": round(over / 1000, 1) if over > 0 else 0.0,
            }
        )
    late = [i for i in items if i["after_interaction_s"] > 0]
    body = table(
        ["agent", "LLM calls", "tokens", "LLM s", "first", "last", "after end s"],
        [list(i.values()) for i in items],
    )
    flag = (
        "**Work continued after the interaction ended** for: "
        + ", ".join(f"{i['agent_id']} (+{i['after_interaction_s']} s)" for i in late)
        if late
        else "No agent outlived the interaction span."
        if interaction_end
        else "No interaction span found, so the overrun check was skipped."
    )
    md = f"{heading(f'Sub-agent fan-out for {trace_id}')}\n\n{body}\n\n{flag}"
    data = {"trace_id": trace_id, "agents": items, "continued_after_end": bool(late)}
    return ToolOutput(md, data)


async def llm_latency(deps: Deps, window: str = "7d") -> ToolOutput:
    ttft, dur = "TRY_CAST(ttft_ms AS DOUBLE)", "TRY_CAST(duration_ms AS DOUBLE)"
    sql = (
        "SELECT COALESCE(model, '(unknown)') AS model, COUNT(*) AS n, "
        f"approx_percentile_cont({ttft}, 0.5) AS ttft_p50, "
        f"approx_percentile_cont({ttft}, 0.95) AS ttft_p95, "
        f"approx_percentile_cont({dur}, 0.5) AS dur_p50, "
        f"approx_percentile_cont({dur}, 0.95) AS dur_p95 "
        f"FROM {deps.claude} WHERE operation_name = {sql_str(tr.OP_LLM)} "
        "GROUP BY model ORDER BY n DESC LIMIT 50"
    )
    rows = await deps.query(sql, "traces", window_range(window, deps.clock()), 50)
    if not rows:
        return empty("LLM latency", f"No llm_request spans in the last {window}.")
    items = [
        {
            "model": str(r.get("model")),
            "calls": int(num(r.get("n"))),
            **{k: _round(r.get(k)) for k in ("ttft_p50", "ttft_p95", "dur_p50", "dur_p95")},
        }
        for r in rows
    ]
    body = table(
        ["model", "calls", "TTFT p50 ms", "TTFT p95 ms", "duration p50 ms", "duration p95 ms"],
        [list(i.values()) for i in items],
    )
    return ToolOutput(
        f"{heading(f'LLM latency, last {window}')}\n\n{body}", {"window": window, "models": items}
    )


def _round(value: Any) -> float | None:
    parsed = to_float(value)
    return None if parsed is None else round(parsed, 1)
