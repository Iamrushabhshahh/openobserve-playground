SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help up down restart logs status health backup reset demo demo-live claude-code claude-code-alerts alerts-log version

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
	@curl -s localhost:5080/config | python3 -c "import json,sys;d=json.load(sys.stdin);print('version:',d['version'],d['build_type'])" 2>/dev/null || echo "version: (not running)"
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

version: ## Show which OpenObserve image is used
	@docker compose config --images
