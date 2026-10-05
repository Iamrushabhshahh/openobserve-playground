# openobserve-playground

A ready-to-run [OpenObserve](https://openobserve.ai) on your laptop, set up for **tracing AI agents**.

Start it, load some example data, and click around. You'll see what an AI coding agent
actually does when you give it a task: every model call, every tool it runs, how long you kept
it waiting, and what it cost. When you're ready, connect your own Claude Code and watch your
real sessions.

![One agent task as a trace: model calls, tools, a helper agent, and time spent waiting for you](docs/images/trace-waterfall-subagent.jpg)

## Try it in 3 steps

You need **Docker**, **make** and **Python 3**.

```bash
git clone https://github.com/Iamrushabhshahh/openobserve-playground.git
cd openobserve-playground

cp .env.example .env      # 1. pick your login (open the file, change the password)
make up                   # 2. start OpenObserve
make demo                 # 3. load 6 hours of example agent activity
```

Now open **<http://localhost:5080>**, log in, and go to **Traces**.

> The example data is made up. It looks exactly like real Claude Code data, so everything
> works the same way. Nothing leaves your machine.

## What to look at

**The traces list.** Each row is one task you gave the agent. You can see how long it took,
how many steps it had, and whether something failed.

![Traces list](docs/images/traces-list.jpg)

**One trace.** Click a row. Each bar is one step: the agent thinking (a model call), running a
tool, or waiting for you to say "yes". Steps inside steps show a helper agent at work, or a
call that went out to another service.

![A trace that crosses into an MCP server and GitHub](docs/images/trace-waterfall-mcp.jpg)

**The service graph.** *Traces → Service Graph → Graph View.* A map of everything the agent
talked to: the AI models, the MCP server, and GitHub behind it.

![Service graph](docs/images/service-graph.jpg)

**Logs next to traces.** Click any step, then the **Logs** tab. You get the log lines from that
moment, right beside the trace.

![Logs for a span](docs/images/trace-to-logs.jpg)

There's a full guided tour, with the queries behind each view, in
**[docs/tracing-tour.md](docs/tracing-tour.md)**.

## Use your own data

| I want to… | Do this |
|---|---|
| See my real Claude Code sessions | `make claude-code`, then start a new Claude Code session. [More →](claude-code/) |
| Track my WHOOP recovery, sleep and strain | Follow [whoop/](whoop/) |
| Send traces from my own app | Point any OpenTelemetry SDK at `http://localhost:5080/api/default`. [How →](docs/settings.md#send-your-own-data) |

## Everyday commands

```text
make up          start OpenObserve
make demo        load example data
make demo-live   keep adding new example tasks (Ctrl+C to stop)
make status      is it running? which version? how much data?
make down        stop (your data is kept)
make reset       delete everything and start fresh
make help        list every command
```

## What's in this folder

```text
docker-compose.yml   OpenObserve, ready for tracing (plus a tiny alert receiver)
Makefile             the commands above
.env.example         copy to .env and set your login
demo/                makes the example agent data
claude-code/         connect your real Claude Code, plus its dashboards and alerts
whoop/               WHOOP health data example
scripts/             small helpers that `make` runs for you
docs/                tracing tour, settings explained, troubleshooting, screenshots
```

## Something not working?

Check **[docs/troubleshooting.md](docs/troubleshooting.md)**. The most common one: OpenObserve
won't start if your password is too simple. Use upper and lower case letters, a number and a
symbol.

Curious why each setting is there, or how to upgrade? See **[docs/settings.md](docs/settings.md)**.

---

MIT licensed. Not affiliated with OpenObserve, Anthropic or WHOOP. All screenshots use made-up data.
