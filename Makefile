SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help quickstart doctor up open down restart logs status health backup reset clean-images demo demo-live claude-code claude-code-off claude-code-alerts alerts-log version mcp-install mcp-test mcp-smoke

O2_PORT := $(or $(shell grep -E '^O2_PORT=' .env 2>/dev/null | cut -d= -f2),5080)
O2_CONTAINER := $(or $(shell grep -E '^O2_CONTAINER=' .env 2>/dev/null | cut -d= -f2),openobserve)
O2_URL := http://localhost:$(O2_PORT)

help: ## List these commands
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-19s\033[0m %s\n", $$1, $$2}'

.env:
	@scripts/init-env.sh

quickstart: ## One command: check, start, load example data, open the UI
	@scripts/doctor.sh
	@$(MAKE) -s up
	@$(MAKE) -s demo
	@$(MAKE) -s open
	@echo "Log in with the email and password in .env, then open Traces -> stream claude_code."

doctor: ## Check Docker, ports, Python and your .env before starting
	@scripts/doctor.sh

open: ## Open the OpenObserve UI in your browser
	@(command -v open >/dev/null && open $(O2_URL)) || (command -v xdg-open >/dev/null && xdg-open $(O2_URL)) || echo "Open $(O2_URL)"

up: .env ## Start OpenObserve (and wait until it's ready)
	docker compose up -d
	@$(MAKE) -s health
	@scripts/setup-service-discovery.sh

down: ## Stop OpenObserve (your data is kept)
	docker compose down

restart: .env ## Restart to apply changes in docker-compose.yml or .env
	docker compose up -d --force-recreate
	@$(MAKE) -s health

logs: ## Show OpenObserve's own logs
	docker compose logs -f --tail=100

status: ## Is it running? Which version? How much data?
	@docker compose ps
	@docker exec $(O2_CONTAINER) /openobserve --version 2>/dev/null || echo "openobserve: not running"
	@du -sh data 2>/dev/null || true

health: ## Wait until OpenObserve answers
	@for i in $$(seq 1 60); do curl -sf $(O2_URL)/healthz >/dev/null && { echo "OpenObserve is up: $(O2_URL)"; exit 0; }; sleep 1; done; echo "OpenObserve did not become healthy in 60 s. Run: make logs  (most common cause: weak ZO_ROOT_USER_PASSWORD)"; exit 1

backup: ## Save a copy of all data into backups/
	@mkdir -p backups
	docker compose stop
	tar -czf backups/o2-data-$$(date +%Y%m%d-%H%M%S).tgz data
	docker compose start
	@$(MAKE) -s health
	@ls -lh backups | tail -1

reset: ## Delete ALL data and start fresh (asks first)
	@read -p "Delete ./data and every stream, dashboard and user in it? [y/N] " a; [ "$$a" = y ]
	docker compose down
	rm -rf data
	@$(MAKE) -s up

demo: .env ## Load 6 hours of example agent activity + dashboards
	python3 demo/generate-agent-traces.py
	scripts/import-dashboards.sh claude-code/dashboards/*.json
	@echo "Open $(O2_URL) -> Traces -> stream claude_code"

demo-live: .env ## Keep adding a new example task every ~20 s (Ctrl+C to stop)
	python3 demo/generate-agent-traces.py --hours 0.5 --tasks 5 --live

claude-code: .env ## Connect your real Claude Code + add its dashboards
	claude-code/connect.sh
	scripts/import-dashboards.sh claude-code/dashboards/*.json
	@echo "Alerts need the claude_code stream: run one Claude Code prompt, then: make claude-code-alerts"

claude-code-off: ## Stop sending Claude Code telemetry (undo make claude-code)
	claude-code/disconnect.sh

claude-code-alerts: .env ## Add the Claude Code alerts (after your first prompt)
	scripts/import-alerts.sh claude-code/alerts/*.json

alerts-log: ## Watch alerts as they arrive
	docker compose logs -f alert-sink

version: ## Show which OpenObserve version and image are used
	@docker exec $(O2_CONTAINER) /openobserve --version 2>/dev/null || echo "openobserve: not running"
	@docker compose config --images | grep -i openobserve

clean-images: ## Remove OpenObserve images no container uses (asks first)
	@used="$$(docker ps -a --format '{{.Image}}' | sort -u)"; \
	unused="$$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -i openobserve | grep -vxF "$$used" || true)"; \
	if [ -z "$$unused" ]; then echo "No unused OpenObserve images."; exit 0; fi; \
	echo "Not used by any container:"; echo "$$unused" | sed 's/^/  /'; \
	read -p "Remove these? [y/N] " a; [ "$$a" = y ] && echo "$$unused" | xargs docker rmi || echo "Kept."

MCP_VENV := mcp/.venv/.installed

$(MCP_VENV): mcp/pyproject.toml
	python3 -m venv mcp/.venv
	mcp/.venv/bin/pip install -q -e ./mcp pytest
	@touch $@

mcp-install: .env ## Install the agent-obs MCP server and register it with Claude Code
	@python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "agent-obs needs Python 3.12+ (python3 is $$(python3 --version))"; exit 1; }
	@$(MAKE) -s $(MCP_VENV)
	@command -v claude >/dev/null || { echo "Installed. Claude Code not found; see mcp/README.md to register it by hand."; exit 0; }
	@set -a; . ./.env; set +a; claude mcp remove agent-obs -s user >/dev/null 2>&1 || true; \
	claude mcp add agent-obs -s user -e O2_URL=$(O2_URL) -e O2_USER="$$ZO_ROOT_USER_EMAIL" -e O2_PASSWORD="$$ZO_ROOT_USER_PASSWORD" -- "$(CURDIR)/mcp/.venv/bin/agent-obs-mcp"
	@echo "agent-obs is registered. In Claude Code, try: \"Using agent-obs, give me the permission wait report for the last 7 days\""

mcp-test: ## Run the agent-obs MCP server's offline tests
	@if command -v uv >/dev/null; then cd mcp && uv run pytest; \
	else $(MAKE) -s $(MCP_VENV) && cd mcp && .venv/bin/python -m pytest; fi

mcp-smoke: .env ## Call every agent-obs MCP tool against the running OpenObserve
	@if command -v uv >/dev/null; then cd mcp && uv run python scripts/smoke.py; \
	else $(MAKE) -s $(MCP_VENV) && cd mcp && .venv/bin/python scripts/smoke.py; fi
