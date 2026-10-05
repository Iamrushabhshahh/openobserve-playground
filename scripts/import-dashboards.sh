#!/usr/bin/env bash
# Import dashboard JSON files into OpenObserve. Skips a dashboard whose title already exists.
#
#   scripts/import-dashboards.sh claude-code/dashboards/*.json
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$here/.env" ] && set -a && . "$here/.env" && set +a
O2_URL="${O2_URL:-http://localhost:5080}"
O2_ORG="${O2_ORG:-default}"
AUTH="${ZO_ROOT_USER_EMAIL:?set ZO_ROOT_USER_EMAIL in .env}:${ZO_ROOT_USER_PASSWORD:?set ZO_ROOT_USER_PASSWORD in .env}"

existing="$(curl -sf -u "$AUTH" "$O2_URL/api/$O2_ORG/dashboards" | python3 -c '
import json, sys
for item in json.load(sys.stdin).get("dashboards", []):
    for value in item.values():
        if isinstance(value, dict) and "title" in value:
            print(value["title"])
')"

for file in "$@"; do
  title="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["title"])' "$file")"
  if grep -Fxq "$title" <<<"$existing"; then
    echo "skip    $title (already exists)"
    continue
  fi
  curl -sf -u "$AUTH" -H 'Content-Type: application/json' \
    -X POST "$O2_URL/api/$O2_ORG/dashboards" --data-binary "@$file" >/dev/null
  echo "import  $title"
done
