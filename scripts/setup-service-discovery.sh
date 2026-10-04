#!/usr/bin/env bash
# Add a "Local / laptop" identity set to OpenObserve's service discovery.
#
# Trace -> "View Logs" and the span "Logs" tab find a service's log stream through service
# discovery. The default identity sets only cover AWS, Azure and Kubernetes, so telemetry
# from a laptop is skipped and the links open an empty stream. This set identifies services
# by service.name + deployment.environment instead. Safe to run again.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$here/.env" ] && set -a && . "$here/.env" && set +a
O2_URL="${O2_URL:-http://localhost:5080}"
O2_ORG="${O2_ORG:-default}"
AUTH="${ZO_ROOT_USER_EMAIL:?set ZO_ROOT_USER_EMAIL in .env}:${ZO_ROOT_USER_PASSWORD:?set ZO_ROOT_USER_PASSWORD in .env}"
url="$O2_URL/api/$O2_ORG/service_streams/config/identity"

current="$(curl -sf -u "$AUTH" "$url")" || { echo "service discovery API not available (needs O2_SERVICE_STREAMS_ENABLED=true)"; exit 0; }
updated="$(python3 -c '
import json, sys
c = json.loads(sys.argv[1])
changed = False
if not any(s.get("id") == "local" for s in c.get("sets", [])):
    c.setdefault("sets", []).append({"id": "local", "label": "Local / laptop", "distinguish_by": ["environment"]})
    changed = True
if "environment" not in c.setdefault("tracked_alias_ids", []):
    c["tracked_alias_ids"].append("environment")
    changed = True
print(json.dumps(c) if changed else "")
' "$current")"

if [ -z "$updated" ]; then
  echo "service discovery: local identity set already present"
else
  curl -sf -u "$AUTH" -X PUT -H 'Content-Type: application/json' "$url" -d "$updated" >/dev/null
  echo "service discovery: added local identity set (services appear within ~10 minutes)"
fi
