SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help up down restart logs status health backup reset claude-code version

help: ## Show targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.env:
	@echo "No .env yet. Run: cp .env.example .env  (then set a password)"; exit 1

up: .env ## Start OpenObserve and wait until it is healthy
	docker compose up -d
	@$(MAKE) -s health

down: ## Stop OpenObserve (data stays in ./data)
	docker compose down

restart: .env ## Recreate the container (applies docker-compose.yml / .env changes)
	docker compose up -d --force-recreate
	@$(MAKE) -s health

logs: ## Follow server logs
	docker compose logs -f --tail=100

status: ## Container state, version and data size
	@docker compose ps
	@curl -s localhost:5080/config | python3 -c "import json,sys;d=json.load(sys.stdin);print('version:',d['version'],d['build_type'])" 2>/dev/null || echo "version: (not running)"
	@du -sh data 2>/dev/null || true

health: ## Wait until /healthz answers
	@for i in $$(seq 1 60); do curl -sf localhost:5080/healthz >/dev/null && { echo "OpenObserve is up: http://localhost:5080"; exit 0; }; sleep 1; done; echo "OpenObserve did not become healthy in 60 s. Run: make logs  (most common cause: weak ZO_ROOT_USER_PASSWORD)"; exit 1

backup: ## Stop, archive ./data into backups/, start again
	@mkdir -p backups
	docker compose stop
	tar -czf backups/o2-data-$$(date +%Y%m%d-%H%M%S).tgz data
	docker compose start
	@$(MAKE) -s health
	@ls -lh backups | tail -1

reset: ## DELETE all data and start empty (asks first)
	@read -p "Delete ./data and every stream, dashboard and user in it? [y/N] " a; [ "$$a" = y ]
	docker compose down
	rm -rf data
	@$(MAKE) -s up

claude-code: .env ## Send Claude Code telemetry here and import its dashboards
	examples/claude-code/setup-claude-telemetry.sh
	scripts/import-dashboards.sh examples/claude-code/dashboards/*.json

version: ## Show the image in use
	@docker compose config --images
