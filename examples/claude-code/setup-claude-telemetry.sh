#!/usr/bin/env bash
# Point Claude Code's built-in OpenTelemetry exporters at OpenObserve.
#
# Writes the "env" block for ~/.claude/settings.json (or prints it) so every
# Claude Code session ships traces, events and metrics to OpenObserve.
#
# Usage:
#   ./setup-claude-telemetry.sh                 # local playground (http://localhost:5080, org "default")
#   CONTENT=1 ./setup-claude-telemetry.sh       # ALSO record prompt text, model responses, tool
#                                               # inputs and tool output (off by default)
#   O2_URL=https://api.openobserve.ai O2_ORG=my_org ./setup-claude-telemetry.sh   # OpenObserve Cloud
#
# Reads ZO_ROOT_USER_EMAIL / ZO_ROOT_USER_PASSWORD from .env (or the environment).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$here/.env" ] && set -a && . "$here/.env" && set +a

O2_URL="${O2_URL:-http://localhost:5080}"
O2_ORG="${O2_ORG:-default}"
O2_STREAM="${O2_STREAM:-claude_code}"
EMAIL="${ZO_ROOT_USER_EMAIL:?set ZO_ROOT_USER_EMAIL in .env}"
PASS="${ZO_ROOT_USER_PASSWORD:?set ZO_ROOT_USER_PASSWORD in .env}"

TOKEN="$(printf '%s:%s' "$EMAIL" "$PASS" | base64 | tr -d '\n')"

# Content capture is opt-in: prompts, responses, tool inputs (commands, file paths) and tool
# output (file contents) end up in OpenObserve. Without it you still get every event, metric
# and span, with the text redacted.
if [ "${CONTENT:-0}" = "1" ]; then LOG_CONTENT=1; else LOG_CONTENT=0; fi

# Endpoint is <host>/api/<org> with NO trailing slash; Claude Code appends /v1/{traces,logs,metrics}.
read -r -d '' ENV_JSON <<EOF || true
{
  "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
  "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
  "OTEL_TRACES_EXPORTER": "otlp",
  "OTEL_LOGS_EXPORTER": "otlp",
  "OTEL_METRICS_EXPORTER": "otlp",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
  "OTEL_EXPORTER_OTLP_ENDPOINT": "${O2_URL}/api/${O2_ORG}",
  "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic ${TOKEN},stream-name=${O2_STREAM}",
  "OTEL_TRACES_EXPORT_INTERVAL": "1000",
  "OTEL_LOGS_EXPORT_INTERVAL": "2000",
  "OTEL_METRIC_EXPORT_INTERVAL": "10000",
  "OTEL_LOG_USER_PROMPTS": "${LOG_CONTENT}",
  "OTEL_LOG_TOOL_DETAILS": "${LOG_CONTENT}",
  "OTEL_LOG_TOOL_CONTENT": "${LOG_CONTENT}",
  "OTEL_METRICS_INCLUDE_REPOSITORY": "true",
  "OTEL_METRICS_INCLUDE_VERSION": "true",
  "OTEL_METRICS_INCLUDE_ENTRYPOINT": "true",
  "OTEL_RESOURCE_ATTRIBUTES": "service.name=claude-code,deployment.environment=demo"
}
EOF

SETTINGS="$HOME/.claude/settings.json"
if command -v python3 >/dev/null 2>&1; then
  python3 - "$SETTINGS" "$ENV_JSON" <<'PY'
import json, os, sys
path, env_json = sys.argv[1], json.loads(sys.argv[2])
os.makedirs(os.path.dirname(path), exist_ok=True)
data = {}
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
data.setdefault("env", {}).update(env_json)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
print(f"updated {path}")
PY
else
  echo "python3 not found; add this to the \"env\" object in $SETTINGS:"
  echo "$ENV_JSON"
fi

cat <<EOF

Done (content capture: $([ "$LOG_CONTENT" = 1 ] && echo ON || echo off; true)).
Start a new Claude Code session and run a prompt. Then in OpenObserve:
  Traces  -> stream "${O2_STREAM}"  : claude_code.interaction > llm_request / tool > blocked_on_user / execution
  Logs    -> stream "${O2_STREAM}"  : claude_code.user_prompt, api_request, tool_result, tool_decision ...
  Metrics -> claude_code_token_usage, claude_code_cost_usage, claude_code_active_time_total ...
Debug with:  claude --debug   (look for [3P telemetry] lines)
EOF
