# agent-obs-mcp

An MCP server that lets Claude Code, Codex or any MCP client investigate **AI coding agents**
the way an SRE investigates a production service. It reads Claude Code (and Codex) telemetry
from OpenObserve.

Ask things like *"where did the time go in my slowest session?"*, *"is the agent stuck in a
loop?"*, *"are we burning our error budget?"*, *"did the last Claude Code upgrade make things
slower?"*, *"when will we run out of budget this month?"* or *"what did the agent run in this
session?"* and get an answer from one tool call.

> Built against the OpenObserve v1.0.4 schema and source. 176 offline tests pass, and
> `make mcp-smoke` passes all 35 calls against a live v1.0.4 instance with real Claude Code data.

## Why another MCP server, when OpenObserve has one?

OpenObserve ships an MCP server inside its binary (`/api/{org}/mcp`): about 140 tools generated
from its OpenAPI spec, found through `tool_search` / `tools_call`, with six pinned tools. It is
an excellent **general interface to OpenObserve**. This server covers the same ground for the
read paths, and adds what it lacks for agent investigations:

| | OpenObserve built-in MCP | agent-obs-mcp |
|---|---|---|
| General access (streams, schema, SQL, values, context, traces, DAG, PromQL, alerts, dashboards, incidents, patterns) | Yes (~140 tools) | Yes, the read paths (18 parity tools) |
| Knows what agent telemetry means (span hierarchy, units, text-typed numbers) | No | Yes: tools and schema resources |
| Trace analysis: self time, critical path, sub-agent fan-out | Raw span lists | Computed |
| Claude Code cost and tokens (no `gen_ai_*` fields) | LLM views show $0 and 0 tokens | Uses Claude Code's own fields |
| Loop / stuck-agent detection | Enterprise-only Agent Signals, not exposed over MCP | `detect_loops` |
| SRE workflows: SLOs and burn rate, regressions by version, incident timelines, budget forecast, alert recommendations, telemetry health, rate limits | No | Yes |
| Compliance: content-free audit trail, risky-action review | No | Yes |
| Multi-agent (Claude Code + Codex) | No | `agents_overview` |
| Resources / prompts | None | 2 resources, 3 prompts |
| Argument completion | Advertised, not implemented | Session ids, windows |
| Writes | Create/update/delete; "requires confirmation" is advisory only | Only `create_alert` / `create_dashboard`: dry-run by default, sent only with `AGENT_OBS_ALLOW_WRITES=1` and `dry_run=false` |
| Prompt and tool content | Returned if queried | Blocked unless `AGENT_OBS_ALLOW_CONTENT=1` |
| Transport | Streamable HTTP inside OpenObserve | stdio, or HTTP with a bearer token for a team |

Use both: OpenObserve's for administering OpenObserve, this one for questions about your agents.

## Install

Python 3.12+. With [uv](https://docs.astral.sh/uv/):

```bash
claude mcp add agent-obs \
  -e O2_URL=http://localhost:5080 \
  -e O2_USER=admin@example.com \
  -e O2_PASSWORD='your-playground-password' \
  -- uv run --directory /path/to/openobserve-playground/mcp agent-obs-mcp
```

Without uv:

```bash
python3 -m venv mcp/.venv && mcp/.venv/bin/pip install -e ./mcp
claude mcp add agent-obs -e O2_USER=... -e O2_PASSWORD=... -- /abs/path/mcp/.venv/bin/agent-obs-mcp
```

**For a team (HTTP):**

```bash
AGENT_OBS_HTTP_TOKEN=<secret> AGENT_OBS_ALLOWED_HOSTS=obs.internal:8766 \
  agent-obs-mcp --transport http --host 0.0.0.0 --port 8766
```

Off loopback it refuses to start without a token and an allowed-hosts list, and keeps
DNS-rebinding protection on.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `O2_URL` | `http://localhost:5080` | OpenObserve base URL |
| `O2_ORG` | `default` | Organization |
| `O2_USER` / `O2_PASSWORD` | – | Basic auth (use a read-only service account in production) |
| `O2_TOKEN` | – | Instead of user/password: the base64 Basic token |
| `CLAUDE_STREAM` | `claude_code` | Claude Code stream (traces and logs) |
| `CODEX_STREAM` | `codex` | Codex log stream, if you export Codex telemetry |
| `AGENT_OBS_TZ` | `Asia/Kolkata` | Time zone for days, "late night" and month boundaries |
| `AGENT_OBS_ALLOW_CONTENT` | off | `1` allows prompt and tool content columns |
| `AGENT_OBS_ALLOW_WRITES` | off | `1` lets `create_alert` / `create_dashboard` send when `dry_run=false` |
| `AGENT_OBS_HTTP_TOKEN`, `AGENT_OBS_ALLOWED_HOSTS` | – | Required for HTTP off loopback |
| `O2_TIMEOUT_S` | `30` | Request timeout |

## Tools (40)

Every tool returns a short markdown answer plus structured data, capped at about 8 KB.

### Agent investigations

| Tool | Ask it |
|---|---|
| `list_sessions` | "My most expensive sessions this week?" |
| `get_trace_tree` | "Where did the time go in this trace? What's the critical path?" |
| `subagent_fanout` | "How much did each sub-agent do? Did work continue after I got my answer?" |
| `permission_wait_report` | "How long did I spend approving tools? What should I allow-list?" |
| `detect_loops` | "Is the agent stuck repeating itself or retrying a failing command?" |
| `compare_sessions` | "Why was this session 3× more expensive than that one?" |
| `cost_report`, `cache_efficiency`, `llm_latency`, `slowest_tools`, `failures` | Spend, cache hit rate, time to first token, p95 per tool, failures with an example trace |
| `agents_overview` | "Claude Code and Codex side by side" |

### SRE for agents

| Tool | Ask it |
|---|---|
| `agent_slo_report` | "Are we meeting a 95% success SLO? Burn rate over 1h/6h/24h/72h: page, ticket or OK?" |
| `regression_check` | "Did time to first token, cost per interaction or failures get worse, and which Claude Code version or model explains it?" |
| `incident_timeline` | "What happened between 14:00 and 16:00? Draft a blameless postmortem." |
| `budget_forecast` | "With a $500 monthly budget, when do we run out and what's a safe daily cap?" |
| `recommend_alerts` | "Which alerts should I have, with thresholds from my real baseline?" (backtested, ready-to-import JSON drafts) |
| `telemetry_health` | "Is my telemetry still arriving? Are traces missing? Is prompt capture on?" |
| `rate_limit_report` | "How much time are 429/529 errors costing us, by model and hour?" |

### Compliance and security

| Tool | Ask it |
|---|---|
| `audit_trail` | "Every action in this session, in order, without any prompt or file content" |
| `risky_actions` | "Permission rejections, bypass mode, sudo/rm/cloud CLIs, MCP servers, ranked by risk" |

### Parity with OpenObserve's MCP (read paths, plus gated writes)

`list_streams`, `stream_schema`, `search_sql`, `search_values`, `search_around`,
`extract_patterns`, `latest_traces`, `trace_dag`, `promql_query`, `promql_range`,
`list_alerts`, `alert_history`, `list_dashboards`, `get_dashboard`, `list_incidents`,
`get_incident`, `create_alert`\*, `create_dashboard`\*, and `run_readonly_sql`.

\* Write tools dry-run by default: they return the exact request they would send.

**Resources:** `agentobs://schema/claude-code`, `agentobs://about`.

**Prompts:** `investigate_slow_session`, `permission_friction_audit`, `weekly_agent_review`.

## Safety

- **Read-only by default.** The two write tools need `AGENT_OBS_ALLOW_WRITES=1` *and*
  `dry_run=false`.
- **Content stays out.** Prompt text, tool input and output, command lines and file paths are
  never selected unless `AGENT_OBS_ALLOW_CONTENT=1`; ad-hoc SQL that names them is rejected.
- **Guarded SQL.** One `SELECT`, no `;`, no comments, no DDL/DML, `LIMIT` at most 500.
- **No credential leaks.** Credentials go only into the `Authorization` header; errors never
  echo them.

## Notes on the numbers

- Trace `start_time`/`end_time` are nanoseconds and `duration` is microseconds. Token and latency
  attributes can arrive as text, so queries use `TRY_CAST`.
- Self time is a span's duration minus the union of its children. The critical path follows,
  from the root, the child that ends last.
- Burn-rate alerting follows the Google SRE workbook's multi-window, multi-burn-rate rules.
- Regressions use a robust z-score (median and MAD) and a two-proportion test for failure rates.
- OpenObserve rejects `GROUP BY` on an alias that shadows a real column name, so queries group by
  the full expression.

## Development

```bash
make mcp-test       # offline tests (no OpenObserve, no network)
make mcp-smoke      # every tool against the running OpenObserve, using ../.env
```
