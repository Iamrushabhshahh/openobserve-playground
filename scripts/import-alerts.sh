#!/usr/bin/env bash
# Create the alert template + destination (the local alert-sink service), then import alert
# JSON files. Skips alerts whose name already exists.
#
#   scripts/import-alerts.sh claude-code/alerts/*.json
#
# Alerts post to ALERT_URL (default: the alert-sink container). Watch them with:
#   docker compose logs -f alert-sink
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$here/.env" ] && set -a && . "$here/.env" && set +a
O2_URL="${O2_URL:-http://localhost:5080}"
O2_ORG="${O2_ORG:-default}"
ALERT_URL="${ALERT_URL:-http://alert-sink:8080/}"
AUTH="${ZO_ROOT_USER_EMAIL:?set ZO_ROOT_USER_EMAIL in .env}:${ZO_ROOT_USER_PASSWORD:?set ZO_ROOT_USER_PASSWORD in .env}"
api() { curl -s -o /dev/null -w '%{http_code}' -u "$AUTH" -H 'Content-Type: application/json' "$@"; }

template='{"name":"playground_alert","type":"http","title":"playground alert","isPrebuilt":false,
 "body":"{\"alert\":\"{alert_name}\",\"stream\":\"{stream_name}\",\"count\":\"{alert_count}\",\"start\":\"{alert_start_time}\",\"end\":\"{alert_end_time}\",\"rows\":\"{rows}\",\"url\":\"{alert_url}\"}"}'
if [ "$(api "$O2_URL/api/$O2_ORG/alerts/templates/playground_alert")" != 200 ]; then
  api -X POST "$O2_URL/api/$O2_ORG/alerts/templates" -d "$template" >/dev/null && echo "create  template playground_alert"
fi

destination="{\"name\":\"playground_alert_sink\",\"type\":\"http\",\"method\":\"post\",\"url\":\"$ALERT_URL\",
 \"template\":\"playground_alert\",\"skip_tls_verify\":false,\"headers\":{\"Content-Type\":\"application/json\"},\"emails\":[],\"metadata\":{}}"
if [ "$(api "$O2_URL/api/$O2_ORG/alerts/destinations/playground_alert_sink")" != 200 ]; then
  code="$(api -X POST "$O2_URL/api/$O2_ORG/alerts/destinations" -d "$destination")"
  [ "$code" = 200 ] || { echo "destination failed (HTTP $code). Is ZO_SKIP_SSRF_CHECKS=true set?"; exit 1; }
  echo "create  destination playground_alert_sink -> $ALERT_URL"
fi

existing="$(curl -sf -u "$AUTH" "$O2_URL/api/v2/$O2_ORG/alerts" | python3 -c 'import json,sys; [print(a["name"]) for a in json.load(sys.stdin).get("list", [])]')"
for file in "$@"; do
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$file")"
  if grep -Fxq "$name" <<<"$existing"; then echo "skip    $name (already exists)"; continue; fi
  code="$(api -X POST "$O2_URL/api/v2/$O2_ORG/alerts" --data-binary "@$file")"
  if [ "$code" = 200 ]; then echo "import  $name"; else
    echo "failed  $name (HTTP $code). The stream must exist first: send some data, then rerun."; fi
done
