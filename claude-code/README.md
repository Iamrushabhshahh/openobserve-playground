# Connect Claude Code

Claude Code can report what it's doing, using OpenTelemetry (the docs are
[here](https://code.claude.com/docs/en/monitoring-usage)). This folder points that at your
playground, so every session shows up in OpenObserve.

## Set it up

```bash
make up            # if the playground isn't running yet
make claude-code   # connects Claude Code and adds three dashboards
```

Then **start a new Claude Code session** (old ones don't pick up the change) and ask it
anything. Within a few seconds you'll see it under **Traces → stream `claude_code`**.

After that first prompt you can also add the alerts:

```bash
make claude-code-alerts
make alerts-log        # watch alerts as they fire
```

## What gets recorded

By default: every step, how long it took, which tool and model were used, tokens and cost.
**Not** recorded: what you typed, what Claude replied, file contents or commands. Those show up
as "redacted".

Want the full text too, for your own debugging? Run:

```bash
CONTENT=1 claude-code/connect.sh
```

Everything stays in your own OpenObserve either way.

## The dashboards

| Dashboard | Answers |
|---|---|
| **Claude Code · Ops** | Is it fast? (time to first token, model call time) Is it reliable? (failed tools, API errors, MCP servers) What does it cost? (per model, per repo) |
| **Claude Code Traces** | Where does the time go: the model, the tools, or waiting for me? |
| **Claude Code Team Productivity** | Cost, tokens, sessions and tool use per person |

## The alerts

| Alert | Tells you when |
|---|---|
| Daily cost | You've spent more than **$50** in the last 24 hours (change the number in `alerts/daily-cost.json`) |
| API errors | 5 or more API errors in 15 minutes |
| MCP failures | An MCP server failed to connect 3 times in 30 minutes |

Alerts go to the small `alert-sink` container, which just prints them. To get them in Slack
instead, change the `playground_alert_sink` destination in the OpenObserve UI
(**Alerts → Destinations**).

The cost is Claude Code's own estimate at list prices. On a subscription plan, it shows the
value of your usage, not your bill.

## Turn it off again

Open `~/.claude/settings.json` and remove the `CLAUDE_CODE_*` and `OTEL_*` lines from the
`env` section.

## Files here

```text
connect.sh        writes the settings into ~/.claude/settings.json
dashboards/       ops.json, traces.json, team-usage.json
alerts/           daily-cost.json, api-errors.json, mcp-failures.json
```

## Using OpenObserve Cloud instead

```bash
O2_URL=https://api.openobserve.ai O2_ORG=<your-org> claude-code/connect.sh
```
