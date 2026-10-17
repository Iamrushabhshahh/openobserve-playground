# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [SemVer](https://semver.org).

## [Unreleased]

### Added
- `AGENTS.md`, so Claude Code, Codex, Cursor, Gemini CLI or Copilot can set the playground up
  step by step, with a check after each step and a list of things to ask first. `CLAUDE.md` and
  `GEMINI.md` point to it, and `/setup` runs it in Claude Code.
- `docs/how-it-works.md` with diagrams: the pieces, one prompt as spans, subagents in one trace,
  what OpenObserve does to every span, and the write and read paths.
- Claude Code · Overview dashboard: sessions, cost per session, waits on a human, rule-approved
  checks, p95/p99 latency and subagents.

### Changed
- New screenshots for the README, the tracing tour and the Claude Code page, taken on a fresh
  clone with example data, plus Overview and Ops dashboard shots. The README now opens with the
  end-to-end diagram.

### Fixed
- The "Minutes waiting on a human" tile counted every permission check, including the ones
  settings rules approve in about a second. It now counts only waits on a person.
- The Claude Code Traces dashboard showed counts as "55.00". Counts now have no decimals;
  minutes, seconds and percentages have one.
- The example data had no rule-approved permission checks, so the Overview showed 0 and
  "No Data" for them. It now records a short wait for those too, as real Claude Code does.

## [1.0.1] - 2026-10-13

### Fixed
- A fresh clone could not start: the default image on `o2cr.ai` needs a registry login. The
  playground now pulls the identical enterprise build from public AWS ECR
  (`public.ecr.aws/zinclabs/openobserve-enterprise:v1.0.4`, same image ID).

## [1.0.0] - 2026-10-13

### Added
- `make quickstart`: checks the machine, creates `.env` with a generated password, starts
  OpenObserve, loads example data and opens the UI.
- `make doctor`: checks Docker, Compose v2, Python, the port and the password rule.
- `O2_PORT` and `O2_CONTAINER` to run next to another OpenObserve.
- `make claude-code-off` to undo `make claude-code`; both back up `~/.claude/settings.json`.
- `make mcp-install` to install and register the agent-obs MCP server with Claude Code.
- CI: script, JSON and compose checks, MCP tests, and a clean-checkout quickstart that
  verifies the example traces are queryable.
- Community files: Code of Conduct, Contributing, Security, Support, Maintainers, issue and
  pull request templates.

## [0.3.0] - 2026-10-09

### Added
- agent-obs MCP server: 40 tools for investigating AI coding agents (SLOs, regressions,
  budgets, loops, audit trail) on top of OpenObserve.
- `claude-code/queries.sql`: the SQL behind every number in the demo.

## [0.2.0] - 2026-10-05

### Added
- Synthetic agent traces (`make demo`, `make demo-live`), a tracing tour and screenshots.
- Claude Code · Ops dashboard, three alerts and an alert receiver.
- Service discovery setup so a trace's "View Logs" finds its log stream.

### Changed
- OpenObserve v1.0.4, privacy-safe Claude Code defaults (content capture is opt-in), simpler
  folders and friendlier docs.

## [0.1.0] - 2026-10-05

### Added
- OpenObserve playground with Claude Code and WHOOP examples.
