"""Static markdown resources: the schema cheat sheet and what this server adds."""

from __future__ import annotations

from .config import Settings


def claude_code_schema(s: Settings) -> str:
    stream = s.claude_stream
    gate = "ON (content allowed)" if s.allow_content else "OFF (content blocked)"
    return f"""# Claude Code telemetry in OpenObserve (stream `{stream}`)

The same stream name holds two stream types. Pick the type with `?type=` on `_search`
(or `stream_type` in `run_readonly_sql`).

## Traces (`type=traces`)

Span hierarchy, one trace per user prompt:

```
claude_code.interaction                 one user turn (root)
├── claude_code.llm_request             model call (model, ttft_ms, duration_ms, *_tokens)
└── claude_code.tool                    one tool use (tool_name)
    ├── claude_code.tool.blocked_on_user   waiting for a permission answer (decision, source)
    └── claude_code.tool.execution         the tool actually running (success, error_class)
```

Sub-agents (Task tool) show up as more llm_request/tool spans with `agent_id` and
`parent_agent_id` set.

| column | meaning / unit |
|---|---|
| trace_id, span_id, reference_parent_span_id | ids; parent link for tree building |
| operation_name | one of the five names above |
| start_time, end_time | **nanoseconds** since epoch |
| duration | **microseconds**, numeric |
| _timestamp | microseconds (span start) |
| session_id, agent_id, parent_agent_id | grouping keys |
| tool_name, model | what ran |
| decision | accept / reject / unknown (blocked_on_user) |
| source | config / user_temporary / user_abort / unknown |
| success | the **strings** 'true' / 'false' |
| ttft_ms, duration_ms, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens | numeric-looking but may be **Utf8** |
| error_class, span_status | failure details |
| user_prompt | **content, gated** |

## Logs (`type=logs`)

`event_name`: api_request, api_error, api_retries_exhausted, tool_result, tool_decision,
user_prompt, mcp_server_connection, …
Columns: cost_usd, model, input_tokens, output_tokens, cache_read_tokens, duration_ms,
tool_name, success, session_id, trace_id, status_code, query_source.

## Gotchas

- Attributes can arrive as Utf8. Always `TRY_CAST(x AS DOUBLE)` / `TRY_CAST(x AS BIGINT)`
  before SUM/AVG/percentiles.
- Compare booleans as text: `CAST(success AS VARCHAR) = 'false'`.
- `COUNT(*) FILTER (WHERE …)` may not be supported: use `SUM(CASE WHEN … THEN 1 ELSE 0 END)`.
- Percentiles: `approx_percentile_cont(TRY_CAST(x AS DOUBLE), 0.95)`.
- Time buckets: `histogram(_timestamp, '1 hour')` (UTC).
- Avoid self-joins; this server joins parent/child spans in Python.
- Content columns ({gate}): user_prompt, prompt, tool_input, tool_parameters,
  full_command, response, content. Set `AGENT_OBS_ALLOW_CONTENT=1` to allow them.

## Example SQL

```sql
-- p95 tool latency (traces)
SELECT tool_name, approx_percentile_cont(TRY_CAST(duration AS DOUBLE), 0.95) / 1000 AS p95_ms
FROM "{stream}" WHERE operation_name = 'claude_code.tool' GROUP BY tool_name

-- cost per model (logs)
SELECT model, SUM(TRY_CAST(cost_usd AS DOUBLE)) AS usd
FROM "{stream}" WHERE event_name = 'api_request' GROUP BY model ORDER BY usd DESC

-- time spent waiting for permission (traces)
SELECT source, decision, COUNT(*) AS waits, SUM(duration) / 1e6 AS seconds
FROM "{stream}" WHERE operation_name = 'claude_code.tool.blocked_on_user'
GROUP BY source, decision
```
"""


ABOUT = """# agent-obs-mcp

A domain-specific MCP server for investigating AI coding agents (Claude Code, Codex)
whose telemetry is stored in OpenObserve.

OpenObserve's built-in MCP server (~140 tools from its OpenAPI spec behind `tool_search` /
`tools_call`) is a general interface to OpenObserve. This server covers the same read paths
and adds what agent investigations need:

- **Answers, not endpoints**: sessions, trace trees with critical path, permission friction,
  loops, sub-agent fan-out, cost, cache, latency and failures as single calls.
- **SRE workflows**: SLOs with multi-window burn rates, regressions attributed to a Claude Code
  version or model, incident timelines with a postmortem draft, budget forecasts, alert
  recommendations from real baselines, telemetry health, rate limits.
- **Compliance**: a content-free audit trail per session and a risky-action review.
- **Multi-agent**: Claude Code and Codex side by side.
- **Safety**: read-only by default; the two write tools dry-run unless
  `AGENT_OBS_ALLOW_WRITES=1`; content blocked unless `AGENT_OBS_ALLOW_CONTENT=1`.
"""
