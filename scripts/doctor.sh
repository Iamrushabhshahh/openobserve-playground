#!/usr/bin/env bash
# Check everything the playground needs before starting, and say exactly what to fix.
set -uo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$here/.env" ] && set -a && . "$here/.env" && set +a
port="${O2_PORT:-5080}"
container="${O2_CONTAINER:-openobserve}"
fail=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mfix\033[0m   %s\n' "$1"; fail=1; }
warn() { printf '  \033[33mnote\033[0m  %s\n' "$1"; }

command -v docker >/dev/null && ok "docker installed" || bad "install Docker Desktop (or Docker Engine): https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 && ok "docker is running" || bad "start Docker, then run this again"
docker compose version >/dev/null 2>&1 && ok "docker compose v2" || bad "install the Docker Compose plugin (docker compose, not docker-compose)"
command -v python3 >/dev/null && ok "python3 $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')" || bad "install Python 3 (used by the helper scripts)"
command -v curl >/dev/null && ok "curl installed" || bad "install curl"

second="set O2_PORT=5090, O2_CONTAINER=openobserve-2 and O2_PROJECT=openobserve-2 in .env"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$container"; then
  owner="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container" 2>/dev/null)"
  if [ -n "$owner" ] && [ "$owner" != "$here" ]; then
    bad "an OpenObserve started from $owner already uses the name '$container': $second"
  else
    ok "OpenObserve container '$container' is already running on port $port"
  fi
elif (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
  bad "port $port is in use by something else: $second"
else
  ok "port $port is free"
fi
if [ "$container" != "openobserve" ] && [ -z "${O2_PROJECT:-}" ]; then
  bad "O2_CONTAINER is set but O2_PROJECT is not: Docker would treat this as your other playground. Add O2_PROJECT=$container to .env"
fi

if [ -n "${ZO_ROOT_USER_PASSWORD:-}" ]; then
  python3 - "$ZO_ROOT_USER_PASSWORD" <<'PY' && ok "password meets OpenObserve's rule" || bad "password in .env is too weak: 8-128 chars with upper, lower, digit and symbol"
import re, sys
p = sys.argv[1]
sys.exit(0 if 8 <= len(p) <= 128 and re.search(r"[a-z]", p) and re.search(r"[A-Z]", p) and re.search(r"\d", p) and re.search(r"[^A-Za-z0-9]", p) else 1)
PY
else
  warn ".env not created yet (make up creates it with a generated password)"
fi
command -v claude >/dev/null && ok "Claude Code installed (for make claude-code)" || warn "Claude Code not found: only needed to send your real sessions"
if [ -d "$here/mcp/.venv" ] && ! "$here/mcp/.venv/bin/python" -c '' 2>/dev/null; then
  warn "the agent-obs MCP install is broken (usually after a Python upgrade): rm -rf mcp/.venv, then make mcp-install"
fi

[ "$fail" = 0 ] && echo "All good." || { echo "Fix the items above, then run: make up"; exit 1; }
