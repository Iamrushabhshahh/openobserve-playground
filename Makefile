SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help up down restart logs status health backup reset clean-images demo demo-live claude-code claude-code-alerts alerts-log version mcp-test mcp-smoke

help: ## List these commands
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-19s\033[0m %s\n", $$1, $$2}'

.env:
	@echo "No .env yet. Run: cp .env.example .env  (then set a password)"; exit 1

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
	@docker exec openobserve /openobserve --version 2>/dev/null || echo "openobserve: not running"
	@du -sh data 2>/dev/null || true

health: ## Wait until OpenObserve answers
	@for i in $$(seq 1 60); do curl -sf localhost:5080/healthz >/dev/null && { echo "OpenObserve is up: http://localhost:5080"; exit 0; }; sleep 1; done; echo "OpenObserve did not become healthy in 60 s. Run: make logs  (most common cause: weak ZO_ROOT_USER_PASSWORD)"; exit 1

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
	@echo "Open http://localhost:5080 -> Traces -> stream claude_code"

demo-live: .env ## Keep adding a new example task every ~20 s (Ctrl+C to stop)
	python3 demo/generate-agent-traces.py --hours 0.5 --tasks 5 --live

claude-code: .env ## Connect your real Claude Code + add its dashboards
	claude-code/connect.sh
	scripts/import-dashboards.sh claude-code/dashboards/*.json
	@echo "Alerts need the claude_code stream: run one Claude Code prompt, then: make claude-code-alerts"

claude-code-alerts: .env ## Add the Claude Code alerts (after your first prompt)
	scripts/import-alerts.sh claude-code/alerts/*.json

alerts-log: ## Watch alerts as they arrive
	docker compose logs -f alert-sink

version: ## Show which OpenObserve version and image are used
	@docker exec openobserve /openobserve --version 2>/dev/null || echo "openobserve: not running"
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

mcp-test: ## Run the agent-obs MCP server's offline tests
	@if command -v uv >/dev/null; then cd mcp && uv run pytest; \
	else $(MAKE) -s $(MCP_VENV) && cd mcp && .venv/bin/python -m pytest; fi

mcp-smoke: .env ## Call every agent-obs MCP tool against the running OpenObserve
	@if command -v uv >/dev/null; then cd mcp && uv run python scripts/smoke.py; \
	else $(MAKE) -s $(MCP_VENV) && cd mcp && .venv/bin/python scripts/smoke.py; fi
