# Contributing

Thanks for helping. Small, focused pull requests are the easiest to review.

## Set up

```bash
git clone https://github.com/Iamrushabhshahh/openobserve-playground.git
cd openobserve-playground
make quickstart      # OpenObserve + example data on http://localhost:5080
```

Already running another OpenObserve? Set `O2_PORT` and `O2_CONTAINER` in `.env` first.

## Make a change

| You're changing | Check it with |
|---|---|
| Makefile, `scripts/`, `docker-compose.yml` | `make quickstart` on a clean clone (or `make reset`), then `make doctor` |
| Dashboards (`claude-code/dashboards/*.json`) | Import with `make demo`, open each panel, then export the JSON back |
| Alerts (`claude-code/alerts/*.json`) | `make claude-code-alerts`, then `make alerts-log` |
| Example data (`demo/`) | `make reset` then `make demo`; check Traces and the dashboards |
| agent-obs MCP (`mcp/`) | `make mcp-test`; with OpenObserve running, `make mcp-smoke` |

CI runs the same checks on every pull request: scripts parse, JSON is valid, the compose file
is valid, the MCP tests pass, and a clean checkout starts and serves the example traces.

## Rules

- **No real data.** Screenshots, fixtures and examples use the demo generator's made-up data.
  Never commit `.env`, `data/`, prompts, tokens or anything from a real session.
- **Docs move with the code.** If a command or a setting changes, update the README or the
  guide in `docs/` in the same pull request, and add a line to [CHANGELOG.md](CHANGELOG.md).
- **Commit messages:** imperative mood, first line under 72 characters
  ("Add a cost panel", not "Added cost panel").

By contributing you agree that your work is released under the [MIT License](LICENSE), and you
follow the [Code of Conduct](CODE_OF_CONDUCT.md).
