# Claude Code → OpenObserve

Claude Code has built-in OpenTelemetry ([docs](https://code.claude.com/docs/en/monitoring-usage)).
This example points it at the playground and adds three dashboards and three alerts.

```bash
make up                   # from the repo root, if not running
make claude-code          # writes the OTEL env block into ~/.claude/settings.json + imports dashboards
# start a NEW Claude Code session and send one prompt, then:
make claude-code-alerts   # imports the alerts (they need the claude_code stream to exist)
make alerts-log           # watch alerts arrive in the alert-sink container
```

## Privacy: content capture is off by default

By default you get every event, metric and span, but **prompt text, model responses, tool inputs
(commands, file paths) and tool output (file contents) are redacted**. To record them too:

```bash
CONTENT=1 examples/claude-code/setup-claude-telemetry.sh
```

Everything stays in **your** OpenObserve. Nothing goes to a third party.

## What the script turns on

| Setting | Effect |
|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY=1` | Metrics and events |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` + `OTEL_TRACES_EXPORTER=otlp` | Traces: interaction → model call / tool → waiting on you / execution |
| `OTEL_METRICS_INCLUDE_REPOSITORY=true` | `vcs_repository_name`, `vcs_owner_name` on metrics (cost per repo) |
| `OTEL_METRICS_INCLUDE_VERSION=true`, `..._ENTRYPOINT=true` | `app_version`, `app_entrypoint` (`cli`, `sdk-cli`, `claude-vscode`, ...) |
| `OTEL_LOG_USER_PROMPTS` / `_TOOL_DETAILS` / `_TOOL_CONTENT` | `0` by default, `1` with `CONTENT=1` |

## Dashboards

| Dashboard | Shows |
|---|---|
| **Claude Code · Ops** | Cost, prompt-cache hit rate, time to first token (p50/p95), model-call duration, where the time goes (model vs tools vs waiting on you), tool calls and failures by tool, slowest tools, cost by model, tokens by type, cost per repository, cost by entry point/version, MCP connections and failures, API errors/retries/refusals, stop reasons, hook time, permission-mode changes |
| Claude Code Traces | Where an agent's time goes, from the trace stream |
| Claude Code Team Productivity | Cost, tokens, sessions, tool usage |

## Alerts

| Alert | Fires when | Window |
|---|---|---|
| `claude_code_daily_cost` | Spend in the last 24 h is above **50 USD** (edit the number in the JSON) | every hour, silence 12 h |
| `claude_code_api_errors` | 5+ API errors or exhausted retries | 15 min |
| `claude_code_mcp_failures` | An MCP server fails to connect 3+ times | 30 min |

Alerts post to the `alert-sink` service in `docker-compose.yml`, which prints them
(`make alerts-log`). To send them to Slack or another webhook instead:
`ALERT_URL=https://hooks.slack.com/... scripts/import-alerts.sh examples/claude-code/alerts/*.json`
on a fresh instance, or edit the `playground_alert_sink` destination in the UI.

`cost_usd` is Claude Code's list-price estimate. On a subscription plan it shows usage value, not
what you are billed.

## What lands where

| Signal | OpenObserve | Examples |
|---|---|---|
| Traces | Traces → stream `claude_code` | `claude_code.interaction` → `llm_request` / `tool` → `blocked_on_user` / `execution` |
| Logs (events) | Logs → stream `claude_code` | `user_prompt`, `api_request`, `tool_result`, `tool_decision`, `mcp_server_connection`, `permission_mode_changed`, `hook_execution_complete` ... |
| Metrics | Metrics → `claude_code_*` | `claude_code_cost_usage`, `claude_code_token_usage`, `claude_code_active_time_total` ... |

## Tips

- **Log line → trace:** in Logs, turn **More → Quick Mode** off (it drops `trace_id` from results),
  expand an event, pick trace stream `claude_code`, click **View Trace**.
- **Text-typed numbers:** some fields (`duration_ms`, `ttft_ms` on spans) arrive as text. Wrap them
  in `TRY_CAST(x AS DOUBLE)` before `SUM` or percentiles, as the Ops dashboard does.
- **Stale panels:** click the dashboard refresh button; the UI caches results.

## Undo

Remove the `CLAUDE_CODE_*` and `OTEL_*` keys from the `env` block in `~/.claude/settings.json`.

## OpenObserve Cloud instead

```bash
O2_URL=https://api.openobserve.ai O2_ORG=<your_org> examples/claude-code/setup-claude-telemetry.sh
```
