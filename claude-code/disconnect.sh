#!/usr/bin/env bash
# Undo connect.sh: remove the OpenTelemetry settings it added to ~/.claude/settings.json.
# A backup of the current file is kept next to it.
set -euo pipefail

SETTINGS="$HOME/.claude/settings.json"
[ -f "$SETTINGS" ] || { echo "No $SETTINGS, nothing to undo."; exit 0; }
cp "$SETTINGS" "$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
keys = ["CLAUDE_CODE_ENABLE_TELEMETRY", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", "OTEL_TRACES_EXPORTER", "OTEL_LOGS_EXPORTER",
        "OTEL_METRICS_EXPORTER", "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_TRACES_EXPORT_INTERVAL", "OTEL_LOGS_EXPORT_INTERVAL", "OTEL_METRIC_EXPORT_INTERVAL", "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_TOOL_CONTENT", "OTEL_METRICS_INCLUDE_REPOSITORY", "OTEL_METRICS_INCLUDE_VERSION",
        "OTEL_METRICS_INCLUDE_ENTRYPOINT", "OTEL_RESOURCE_ATTRIBUTES"]
data = json.load(open(path))
env = data.get("env", {})
removed = [k for k in keys if env.pop(k, None) is not None]
json.dump(data, open(path, "w"), indent=2)
print(f"removed {len(removed)} telemetry settings from {path}")
PY
echo "Claude Code no longer sends telemetry. New sessions pick this up."
