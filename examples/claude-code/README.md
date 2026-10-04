# Claude Code → OpenObserve

Claude Code has built-in OpenTelemetry. This example points it at the playground and imports two
dashboards.

```bash
make up            # from the repo root, if not running
make claude-code   # writes the OTEL env block into ~/.claude/settings.json + imports dashboards
```

Start a **new** Claude Code session and send a prompt. Data appears within a few seconds.

## What lands where

| Signal | OpenObserve | Examples |
|---|---|---|
| Traces | Traces → stream `claude_code` | `claude_code.interaction` → `llm_request` / `tool` → `blocked_on_user` / `execution` |
| Logs (events) | Logs → stream `claude_code` | `user_prompt`, `api_request`, `tool_result`, `tool_decision` |
| Metrics | Metrics | `claude_code_token_usage`, `claude_code_cost_usage`, `claude_code_active_time_total` |

## Dashboards

| File | Shows |
|---|---|
| `dashboards/claude-code-traces.dashboard.json` | Where an agent's time goes: model time vs tool execution vs waiting for you |
| `dashboards/claude-code-team-productivity.community.dashboard.json` | Cost, tokens, sessions, tool usage |

## Privacy

The script turns on `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` and `OTEL_LOG_TOOL_CONTENT`.
Your prompts and tool output are then stored in **your local** OpenObserve. To keep them out, set
those three to `"0"` in `~/.claude/settings.json`.

## Undo

Remove the `CLAUDE_CODE_ENABLE_TELEMETRY` and `OTEL_*` keys from the `env` block in
`~/.claude/settings.json`.

## OpenObserve Cloud instead

```bash
O2_URL=https://api.openobserve.ai O2_ORG=<your_org> ./setup-claude-telemetry.sh
```
