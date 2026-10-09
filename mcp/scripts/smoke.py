"""Call every tool against a live OpenObserve and print PASS/FAIL per tool.

Credentials come from O2_USER/O2_PASSWORD/O2_TOKEN, or else from the playground's .env
(ZO_ROOT_USER_EMAIL / ZO_ROOT_USER_PASSWORD). They are never printed.

    make mcp-smoke          # from the playground root
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

from agent_obs_mcp.config import load_settings
from agent_obs_mcp.server import build_server

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
ENV_MAP = {"ZO_ROOT_USER_EMAIL": "O2_USER", "ZO_ROOT_USER_PASSWORD": "O2_PASSWORD"}
FIND_SESSIONS_SQL = (
    'SELECT session_id, COUNT(*) AS n FROM "{stream}" WHERE session_id IS NOT NULL '
    "GROUP BY session_id ORDER BY n DESC"
)
FIND_TRACE_SQL = (
    "SELECT trace_id, duration FROM \"{stream}\" WHERE operation_name = 'claude_code.interaction' "
    "ORDER BY duration DESC"
)


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def build_env() -> dict[str, str]:
    env = dict(os.environ)
    if env.get("O2_TOKEN") or (env.get("O2_USER") and env.get("O2_PASSWORD")):
        return env
    file_env = read_env_file(ENV_FILE)
    for src, dst in ENV_MAP.items():
        if file_env.get(src):
            env.setdefault(dst, file_env[src])
    return env


async def call(server: Any, name: str, args: dict[str, Any]) -> tuple[bool, str, Any]:
    try:
        result = await server.call_tool(name, args)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", None
    text = result.content[0].text if result.content else ""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return not result.isError, first[:110], result.structuredContent


async def find_trace_id(server: Any, stream: str) -> str | None:
    ok, _, data = await call(
        server,
        "run_readonly_sql",
        {"sql": FIND_TRACE_SQL.format(stream=stream), "stream_type": "traces", "window": "30d",
         "limit": 1},
    )  # fmt: skip
    rows = (data or {}).get("rows") or []
    return str(rows[0]["trace_id"]) if ok and rows else None


async def find_session_ids(server: Any, stream: str) -> list[str]:
    ok, _, data = await call(
        server,
        "run_readonly_sql",
        {"sql": FIND_SESSIONS_SQL.format(stream=stream), "stream_type": "logs", "window": "30d",
         "limit": 2},
    )  # fmt: skip
    rows = (data or {}).get("rows") or []
    return [str(r["session_id"]) for r in rows] if ok else []


def extra_plan(stream: str, sessions: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Parity, agent-behaviour and SRE tools; write tools only in dry-run mode."""
    plan: list[tuple[str, dict[str, Any]]] = [
        ("list_streams", {"stream_type": "all"}),
        ("stream_schema", {"stream": stream, "stream_type": "traces"}),
        ("search_sql", {"sql": f'SELECT COUNT(*) AS n FROM "{stream}"', "window": "24h"}),
        ("search_values", {"stream": stream, "fields": ["event_name"], "window": "7d"}),
        ("latest_traces", {"stream": stream, "window": "7d", "limit": 5}),
        ("promql_query", {"query": "claude_code_cost_usage"}),
        ("list_alerts", {}),
        ("alert_history", {"window": "7d"}),
        ("list_dashboards", {}),
        ("list_incidents", {"window": "7d"}),
        ("detect_loops", {"window": "7d"}),
        ("risky_actions", {"window": "7d"}),
        ("agents_overview", {"window": "7d"}),
        ("agent_slo_report", {"window": "7d"}),
        ("regression_check", {"metric": "ttft", "baseline": "7d", "recent": "24h"}),
        ("incident_timeline", {"window": "24h"}),
        ("budget_forecast", {"monthly_budget_usd": 500}),
        ("recommend_alerts", {"window": "14d"}),
        ("telemetry_health", {}),
        ("rate_limit_report", {"window": "7d"}),
        ("create_alert", {"name": "smoke_dry_run", "stream": stream,
                          "sql": f'SELECT COUNT(*) AS n FROM "{stream}"', "threshold": 1,
                          "destinations": ["playground_alert_sink"]}),
    ]  # fmt: skip
    if sessions:
        plan.append(("audit_trail", {"session_id": sessions[0]}))
    if len(sessions) > 1:
        plan.append(("compare_sessions", {"session_a": sessions[0], "session_b": sessions[1]}))
    return plan


async def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings(build_env())
    server = build_server(settings)
    print(f"OpenObserve: {settings.safe_url}  org={settings.org}  stream={settings.claude_stream}")
    trace_id = await find_trace_id(server, settings.claude_stream)
    plan: list[tuple[str, dict[str, Any]]] = [
        ("list_sessions", {"window": "30d"}),
        ("permission_wait_report", {"window": "30d"}),
        ("cost_report", {"window": "30d", "by": "model"}),
        ("cost_report", {"window": "30d", "by": "day"}),
        ("cache_efficiency", {"window": "30d"}),
        ("slowest_tools", {"window": "30d"}),
        ("failures", {"window": "30d"}),
        ("llm_latency", {"window": "30d"}),
        ("run_readonly_sql", {"sql": f'SELECT COUNT(*) AS n FROM "{settings.claude_stream}"'}),
    ]
    sessions = await find_session_ids(server, settings.claude_stream)
    plan += extra_plan(settings.claude_stream, sessions)
    if trace_id:
        plan += [
            ("get_trace_tree", {"trace_id": trace_id}),
            ("subagent_fanout", {"trace_id": trace_id}),
            (
                "trace_dag",
                {"stream": settings.claude_stream, "trace_id": trace_id, "window": "30d"},
            ),
        ]
    failed = 0
    for name, args in plan:
        ok, summary, _ = await call(server, name, args)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<24} {summary}")
    if not trace_id:
        failed += 1
        print("FAIL  get_trace_tree/subagent_fanout  no interaction trace in the last 30d")
    total = len(plan) + (0 if trace_id else 1)
    print(f"\n{total - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
