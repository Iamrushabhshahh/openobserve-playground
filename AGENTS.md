# Setting up the playground for someone

You're an AI coding agent (Claude Code, Codex, Cursor, Gemini CLI, Copilot or similar), and
someone has asked you to set this repo up for them. This file is how I'd like you to do it.

Assume the person is new to OpenTelemetry and OpenObserve. Do the work, but explain each step
in a sentence as you go, so they finish knowing what's running on their laptop and why. Don't
dump the whole plan up front.

## What this repo is

A single OpenObserve container on the person's laptop, set up for tracing AI coding agents.
It can show example agent data, their own Claude Code sessions, or traces from their own app.
Everything stays on their machine. The [README](README.md) is the human version of this file.

## Ask first, every time

These change things outside the repo or can't be undone. Say what will happen and wait for a
yes:

- `make claude-code` writes OpenTelemetry settings into `~/.claude/settings.json`. It backs the
  file up first, but it's still their config.
- `CONTENT=1 make claude-code` also records prompt text, tool inputs and file contents. Only if
  they ask for it by name.
- `make reset` deletes every stream, dashboard and user.
- `make mcp-install` registers an MCP server with their Claude Code.
- Changing `O2_PORT` or `O2_CONTAINER` when they already run another OpenObserve.

Never print the password from `.env` in the chat. Tell them it's in `.env`, next to
`ZO_ROOT_USER_EMAIL`. Never commit `.env`.

## The steps

Run each command from the repo root, check the result, then move on. If a check fails, stop and
fix it before the next step. [docs/troubleshooting.md](docs/troubleshooting.md) covers the
usual ones.

1. **Check the machine.** Run `make doctor`. It checks Docker, Compose v2, Python 3, curl and
   whether port 5080 is free. Fix anything marked `fix`. If 5080 is taken by something else,
   add `O2_PORT=5090` and `O2_CONTAINER=openobserve-2` to `.env` (after asking) and use that
   port everywhere below.

2. **Start OpenObserve.** Run `make up`. The first run pulls a ~620 MB image. On first start it
   creates `.env` with a generated password. It's done when it prints
   `OpenObserve is up: http://localhost:5080`. If it times out, run `make logs`. The usual cause
   is a password OpenObserve thinks is too weak.

3. **Ask what they want to see.** Pick one with them:
   - **Example data**, if they just want to look around. Go to step 4.
   - **Their own Claude Code sessions.** Go to step 5.
   - **Their own app.** Go to step 6.

4. **Example data.** Run `make demo`. It loads six hours of made-up Claude Code activity and
   imports the dashboards. Check it landed (step 7).

5. **Their own Claude Code.** After they say yes, run `make claude-code`. Then tell them to start
   a *new* Claude Code session (running sessions don't pick up the change) and send one prompt.
   Spans show up within a few seconds. Check with step 7. After that first prompt,
   `make claude-code-alerts` adds the three alerts if they want them.

6. **Their own app.** Point them at [docs/settings.md](docs/settings.md#send-your-own-data). Any
   OpenTelemetry SDK works with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:5080/api/default`
   and a Basic auth header built from the login in `.env`. Help them set the variables for their
   language, then check with step 7.

7. **Check the data is there.** This lists trace streams and their span counts without printing
   the password:

   ```bash
   set -a; . ./.env; set +a
   curl -s -u "$ZO_ROOT_USER_EMAIL:$ZO_ROOT_USER_PASSWORD" \
     "http://localhost:${O2_PORT:-5080}/api/default/streams?type=traces" \
     | python3 -c 'import json,sys; [print(s["name"], s["stats"]["doc_num"], "spans") for s in json.load(sys.stdin)["list"]]'
   ```

   You want a `claude_code` stream (or their app's stream) with more than zero spans. Counts can
   lag a few seconds behind.

8. **Open it and show them around.** Run `make open`, or give them the URL. They log in with the
   email and password from `.env`. Then point them at:
   - **Traces → stream `claude_code`.** Each row is one task. Click one to see the waterfall.
   - **Dashboards → Claude Code · Overview.** Sessions, cost, waits on a human, latency, subagents.
   - **Dashboards → Claude Code Traces.** Where the time goes: model, tools, or waiting for them.
   - [docs/tracing-tour.md](docs/tracing-tour.md), if they want the guided version.

## When you're done, tell them

- The URL, and that the login is in `.env`.
- What you loaded: example data, their Claude Code, or their app.
- How to stop it: `make down` keeps the data; `make claude-code-off` stops sending Claude Code
  telemetry and restores their settings.
- One next thing to try. [docs/how-it-works.md](docs/how-it-works.md) explains what just
  happened under the hood, and `make mcp-install` lets Claude answer questions about its own
  traces.

## Where things are

```text
Makefile                 every command; `make help` lists them
docker-compose.yml       OpenObserve plus a tiny alert receiver
scripts/                 doctor, .env creation, dashboard and alert import
demo/                    the example data generator
claude-code/             connect.sh, dashboards, alerts, queries.sql
mcp/                     agent-obs, an MCP server for asking questions about agent traces
docs/                    tracing tour, how it works, settings, troubleshooting, screenshots
whoop/                   an optional extra; skip it unless they have a WHOOP band
```
