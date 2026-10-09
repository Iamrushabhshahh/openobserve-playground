from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import FIXTURES, NOW_S, TEST_ENV

from agent_obs_mcp import parity_tools as pt
from agent_obs_mcp.client import O2Client
from agent_obs_mcp.config import load_settings
from agent_obs_mcp.deps import Deps
from agent_obs_mcp.server import build_server
from agent_obs_mcp.sqlguard import SqlGuardError

pytestmark = pytest.mark.anyio

NOW_US = int(NOW_S * 1_000_000)
ORG = "/api/default"
V2 = "/api/v2/default"
KSUID = "2abcdefghijklmnopqrstuvwxyz"


class Router:
    """Answers by (method, path); unknown routes get a 404 like a missing endpoint."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None):
        self.routes = routes or {}
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
                "body": body,
            }
        )
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"code": 404, "message": "not found"})
        status, payload = route if isinstance(route, tuple) else (200, route)
        return httpx.Response(status, json=payload)

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def deps_for(routes: dict[tuple[str, str], Any] | None = None, **env: str) -> tuple[Deps, Router]:
    router = Router(routes)
    settings = load_settings({**TEST_ENV, **env})
    client = O2Client(settings, httpx.MockTransport(router))
    return Deps(settings=settings, client=client, clock=lambda: NOW_S), router


def server_for(routes: dict[tuple[str, str], Any] | None = None, **env: str):
    router = Router(routes)
    server = build_server(
        load_settings({**TEST_ENV, **env}), httpx.MockTransport(router), clock=lambda: NOW_S
    )
    return server, router


async def test_list_streams_all_types_sorted_by_docs() -> None:
    deps, router = deps_for({("GET", f"{ORG}/streams"): fixture("parity_streams.json")})
    out = await pt.list_streams_impl(deps)
    assert router.last["params"] == {}
    assert [s["name"] for s in out.data["streams"]] == ["default", "small"]
    assert out.data["streams"][0]["storage_mb"] == 12.35
    assert "| default | traces | 12345 | 12.35 | 40 |" in out.markdown


async def test_list_streams_passes_type_and_handles_empty() -> None:
    deps, router = deps_for({("GET", f"{ORG}/streams"): {"list": [], "total": 0}})
    out = await pt.list_streams_impl(deps, "metrics", keyword="cpu")
    assert router.last["params"] == {"type": "metrics", "keyword": "cpu"}
    assert "No data" in out.markdown


async def test_stream_schema_flags_utf8_numbers() -> None:
    schema = {
        "name": "claude_code",
        "schema": [
            {"name": "_timestamp", "type": "Int64"},
            {"name": "cost_usd", "type": "Utf8"},
            {"name": "duration_ms", "type": "Utf8"},
            {"name": "model", "type": "Utf8"},
        ],
        "uds_schema": [{"name": "model", "type": "Utf8"}],
    }
    deps, router = deps_for({("GET", f"{ORG}/streams/claude_code/schema"): schema})
    out = await pt.stream_schema_impl(deps, "claude_code", "traces")
    assert router.last["params"] == {"type": "traces"}
    assert "`cost_usd`, `duration_ms`" in out.markdown and "TRY_CAST" in out.markdown
    assert "`model`" not in out.data["notes"][1]
    assert "only 1 fields" in out.markdown
    assert len(out.data["fields"]) == 4


async def test_stream_schema_rejects_path_injection() -> None:
    deps, _ = deps_for()
    with pytest.raises(ValueError):
        await pt.stream_schema_impl(deps, "../users")


async def test_search_sql_any_stream_local_csv_and_content_stripped() -> None:
    hits = [{"level": "error", "body": "secret", "n": 3}, {"level": "warn", "body": "x", "n": 1}]
    deps, router = deps_for({("POST", f"{ORG}/_search"): {"hits": hits, "total": 2, "took": 4}})
    out = await pt.search_sql_impl(deps, "SELECT * FROM k8s_logs", "logs", "2h", 50, "csv")
    sent = router.last
    assert sent["params"] == {"type": "logs"}
    assert sent["body"]["query"]["sql"] == "SELECT * FROM k8s_logs LIMIT 50"
    assert (
        sent["body"]["query"]["end_time"] - sent["body"]["query"]["start_time"] == 2 * 3600 * 10**6
    )
    assert "agent_options" not in sent["body"]
    assert "```csv\nlevel,n\nerror,3\nwarn,1\n```" in out.markdown
    assert all("body" not in r for r in out.data["rows"])


async def test_search_sql_server_side_format_when_content_allowed() -> None:
    payload = {"hits": [], "format": "csv", "data": "a,b\n1,2\n3,4", "total": 2}
    deps, router = deps_for({("POST", f"{ORG}/_search"): payload}, AGENT_OBS_ALLOW_CONTENT="1")
    out = await pt.search_sql_impl(deps, 'SELECT a, b FROM "x"', "metrics", "1h", 10, "csv")
    assert router.last["body"]["agent_options"] == {"output_format": "csv"}
    assert router.last["params"] == {"type": "metrics"}
    assert "2 rows" in out.markdown and "a,b\n1,2" in out.markdown
    assert out.data["formatted"] == "a,b\n1,2\n3,4"


async def test_search_sql_md_table_and_json_formats() -> None:
    hits = [{"k": "v|w", "n": 1}]
    deps, _ = deps_for({("POST", f"{ORG}/_search"): {"hits": hits}})
    md = await pt.search_sql_impl(deps, "SELECT k, n FROM s", output_format="md_table")
    assert "| v\\|w | 1 |" in md.markdown
    js = await pt.search_sql_impl(deps, "SELECT k, n FROM s", output_format="json")
    assert '```json\n{"k": "v|w", "n": 1}\n```' in js.markdown


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE logs",
        "SELECT * FROM a; DELETE FROM a",
        "SELECT 1 -- no",
        "SELECT prompt FROM claude_code",
        "SELECT 1",
    ],
)
async def test_search_sql_guard_rejects(sql: str) -> None:
    deps, router = deps_for()
    with pytest.raises(SqlGuardError):
        await pt.search_sql_impl(deps, sql)
    assert router.requests == []


async def test_search_sql_caps_limit() -> None:
    deps, router = deps_for({("POST", f"{ORG}/_search"): {"hits": []}})
    out = await pt.search_sql_impl(deps, "SELECT * FROM s LIMIT 9999", limit=500)
    assert router.last["body"]["query"]["sql"].endswith("LIMIT 500")
    assert "No rows." in out.markdown


async def test_search_values_request_and_render() -> None:
    values = {
        "took": 3,
        "values": [
            {
                "field": "service_name",
                "values": [
                    {"zo_sql_key": "api", "zo_sql_num": 40},
                    {"zo_sql_key": "db", "zo_sql_num": 2},
                ],
            },
            {"field": "empty", "values": []},
        ],
    }
    deps, router = deps_for({("GET", f"{ORG}/default/_values"): values})
    out = await pt.search_values_impl(deps, "default", ["service_name", "empty"], "traces", "6h", 5)
    params = router.last["params"]
    assert params["fields"] == "service_name,empty" and params["size"] == "5"
    assert params["type"] == "traces" and params["end_time"] == str(NOW_US)
    assert "| api | 40 |" in out.markdown
    assert [g["field"] for g in out.data["fields"]] == ["service_name"]


async def test_search_values_blocks_content_fields() -> None:
    deps, _ = deps_for()
    with pytest.raises(ValueError, match="blocked"):
        await pt.search_values_impl(deps, "claude_code", ["user_prompt"])
    with pytest.raises(ValueError, match="invalid field"):
        await pt.search_values_impl(deps, "claude_code", ["a b"])


async def test_search_around_marks_anchor() -> None:
    hits = [
        {"_timestamp": 30, "log": "after"},
        {"_timestamp": 20, "log": "anchor", "prompt": "hidden"},
        {"_timestamp": 10, "log": "before"},
    ]
    deps, router = deps_for({("GET", f"{ORG}/app/_around"): {"hits": hits}})
    out = await pt.search_around_impl(deps, "app", 20, 3)
    assert router.last["params"] == {"type": "logs", "key": "20", "size": "3"}
    assert [r["log"] for r in out.data["rows"]] == ["before", "anchor", "after"]
    assert "| ★ | 20 | anchor |" in out.markdown
    assert "hidden" not in out.markdown


async def test_latest_traces_sorted_by_duration_without_content() -> None:
    deps, router = deps_for(
        {("GET", f"{ORG}/default/traces/latest"): fixture("parity_traces_latest.json")}
    )
    out = await pt.latest_traces_impl(deps, "default", "1h", 20, "service_name='api'")
    params = router.last["params"]
    assert params["sort_by"] == "duration" and params["sort_order"] == "desc"
    assert params["filter"] == "service_name='api'"
    first = out.data["traces"][0]
    assert first["trace_id"] == "t-slow" and first["duration_ms"] == 2500.0
    assert first["spans"] == 14 and first["errors"] == 2
    assert first["start_us"] == 1_791_457_200_000_000
    assert "gen_ai_input_messages" not in first
    assert "| t-slow | api | GET /orders | 2,500 | 14 | 2 |" in out.markdown


@pytest.mark.parametrize("bad", ["a=1; DROP x", "a=1 UNION SELECT 1", "a=1 -- x"])
async def test_latest_traces_rejects_unsafe_filter(bad: str) -> None:
    deps, router = deps_for()
    with pytest.raises(ValueError):
        await pt.latest_traces_impl(deps, filter=bad)
    assert router.requests == []


async def test_trace_dag_renders_tree() -> None:
    deps, router = deps_for(
        {("GET", f"{ORG}/default/traces/abc123/dag"): fixture("parity_dag.json")}
    )
    out = await pt.trace_dag_impl(deps, "default", "abc123")
    assert router.last["params"]["end_time"] == str(NOW_US)
    lines = out.markdown.splitlines()[2:]
    assert lines == [
        "- api: GET /orders — 1,000 ms",
        "  - cache: GET key — 10 ms",
        "  - db: SELECT orders — 300 ms **ERROR**",
    ]
    assert "3 spans, 1 errors" in out.markdown


async def test_trace_dag_validates_trace_id() -> None:
    deps, _ = deps_for()
    with pytest.raises(ValueError):
        await pt.trace_dag_impl(deps, "default", "abc/../x")


async def test_promql_instant_vector() -> None:
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"__name__": "up", "job": "api"}, "value": [1.0, "1"]},
                {"metric": {"__name__": "up", "job": "db"}, "value": [1.0, "0"]},
            ],
        },
    }
    deps, router = deps_for({("GET", f"{ORG}/prometheus/api/v1/query"): payload})
    out = await pt.promql_query_impl(deps, "up", "1791460800")
    assert router.last["params"] == {"query": "up", "time": "1791460800"}
    assert '| up{job="api"} | 1 |' in out.markdown
    assert out.data["series"][1]["value"] == 0.0


async def test_promql_scalar_result() -> None:
    payload = {"status": "success", "data": {"resultType": "scalar", "result": [1.0, "42"]}}
    deps, _ = deps_for({("GET", f"{ORG}/prometheus/api/v1/query"): payload})
    out = await pt.promql_query_impl(deps, "scalar(sum(up))")
    assert out.data["series"] == [{"labels": "scalar", "value": 42.0}]


async def test_promql_range_summarises_series() -> None:
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"job": "a"}, "values": [[1, "1"], [2, "3"]]},
                {"metric": {"job": "b"}, "values": [[1, "10"], [2, "NaN"], [3, "20"]]},
            ],
        },
    }
    deps, router = deps_for({("GET", f"{ORG}/prometheus/api/v1/query_range"): payload})
    out = await pt.promql_range_impl(deps, "rate(x[5m])", "1h")
    params = router.last["params"]
    assert params["step"] == "15s"
    assert int(params["end"]) - int(params["start"]) == 3600
    first = out.data["series"][0]
    assert first["labels"] == '{job="b"}'
    assert first["min"] == 10.0 and first["max"] == 20.0 and first["last"] == 20.0


async def test_promql_empty_query_rejected() -> None:
    deps, _ = deps_for()
    with pytest.raises(ValueError):
        await pt.promql_query_impl(deps, "  ")


async def test_list_alerts_v2_path() -> None:
    alerts = {
        "list": [
            {
                "alert_id": KSUID,
                "name": "api_errors",
                "alert_type": "scheduled",
                "stream_name": "default",
                "stream_type": "logs",
                "enabled": True,
                "is_real_time": False,
                "last_outcome": "firing",
                "last_triggered_at": NOW_US,
                "folder_id": "default",
                "folder_name": "Default",
            }
        ]
    }
    deps, router = deps_for({("GET", f"{V2}/alerts"): alerts})
    out = await pt.list_alerts_impl(deps)
    assert router.last["path"] == f"{V2}/alerts"
    assert "| api_errors | scheduled | default | yes | firing | 2026-10-08 12:00 | Default |" in (
        out.markdown
    )


async def test_alert_history_by_name_and_by_id() -> None:
    hits = [
        {"timestamp": NOW_US, "alert_name": "a1", "status": "firing", "retries": 0},
        {"timestamp": NOW_US - 1, "alert_name": "a1", "status": "firing", "retries": 0},
        {"timestamp": NOW_US - 2, "alert_name": "a2", "status": "error", "error": "boom"},
    ]
    deps, router = deps_for({("GET", f"{V2}/alerts/history"): {"total": 3, "hits": hits}})
    out = await pt.alert_history_impl(deps, "24h", "a1")
    assert "alert_id" not in router.last["params"]
    assert out.data["summary"] == [{"alert": "a1", "status": "firing", "count": 2}]
    await pt.alert_history_impl(deps, "24h", KSUID, 50)
    assert router.last["params"]["alert_id"] == KSUID
    assert router.last["params"]["size"] == "50"
    all_out = await pt.alert_history_impl(deps)
    assert "| a2 | error | 1 |" in all_out.markdown and "boom" in all_out.markdown


async def test_list_dashboards_scope() -> None:
    boards = {
        "dashboards": [
            {
                "dashboard_id": "ops",
                "title": "Ops",
                "description": "",
                "owner": "me",
                "folder_id": "default",
                "folder_name": "default",
                "v8": {"huge": "x" * 50_000},
            }
        ]
    }
    deps, router = deps_for({("GET", f"{ORG}/dashboards"): boards})
    out = await pt.list_dashboards_impl(deps)
    assert router.last["params"] == {}
    assert "default folder" in out.markdown and "| ops | Ops | default | me |" in out.markdown
    assert "huge" not in json.dumps(out.data)
    await pt.list_dashboards_impl(deps, title="op")
    assert router.last["params"] == {"title": "op"}


async def test_get_dashboard_summarises_panels() -> None:
    deps, _ = deps_for({("GET", f"{ORG}/dashboards/ops"): fixture("parity_dashboard.json")})
    out = await pt.get_dashboard_impl(deps, "ops")
    assert "Dashboard: Ops" in out.markdown and "2 panels" in out.markdown
    assert "- [Default] **Up** (metric) `up`" in out.markdown
    assert out.data["panels"][0]["streams"] == ["default"]


async def test_incidents_enterprise_happy_path() -> None:
    incidents = {
        "incidents": [
            {
                "id": "inc1",
                "status": "open",
                "severity": "P2",
                "title": "API down",
                "alert_count": 3,
                "first_alert_at": NOW_US - 3_600_000_000,
                "last_alert_at": NOW_US - 60_000_000,
            },
            {"id": "old", "status": "resolved", "last_alert_at": NOW_US - 30 * 86_400 * 10**6},
        ],
        "total": 2,
    }
    detail = {
        **incidents["incidents"][0],
        "triggers": [
            {"alert_id": "a", "alert_name": "api_errors", "alert_fired_at": NOW_US,
             "correlation_reason": "temporal"},
        ],
        "alerts": [],
    }  # fmt: skip
    deps, router = deps_for(
        {
            ("GET", f"{V2}/alerts/incidents"): incidents,
            ("GET", f"{V2}/alerts/incidents/inc1"): detail,
        }
    )
    out = await pt.list_incidents_impl(deps, "7d", "open")
    assert router.last["params"]["status"] == "open"
    assert [i["id"] for i in out.data["incidents"]] == ["inc1"]
    one = await pt.get_incident_impl(deps, "inc1")
    assert "status **open**" in one.markdown and "| api_errors |" in one.markdown


async def test_incidents_open_source_edition_is_clean() -> None:
    deps, _ = deps_for({("GET", f"{V2}/alerts/incidents"): {"incidents": [], "total": 0}})
    listed = await pt.list_incidents_impl(deps)
    assert "always returns an empty list" in listed.markdown
    forbidden, _ = deps_for(
        {("GET", f"{V2}/alerts/incidents/inc1"): (403, {"code": 403, "message": "Not Supported"})}
    )
    one = await pt.get_incident_impl(forbidden, "inc1")
    assert "not available on this edition" in one.markdown
    assert one.data["available"] is False


async def test_enterprise_404_via_server_is_not_an_error() -> None:
    server, _ = server_for()
    result = await server.call_tool("list_incidents", {"window": "7d"})
    assert not result.isError
    assert "not available on this edition" in result.content[0].text
    patterns = await server.call_tool("extract_patterns", {"stream": "app"})
    assert not patterns.isError and "ExtractPatterns" in patterns.content[0].text


async def test_enterprise_500_is_still_an_error() -> None:
    server, _ = server_for({("GET", f"{V2}/alerts/incidents"): (500, {"message": "db down"})})
    result = await server.call_tool("list_incidents", {})
    assert result.isError and "HTTP 500" in result.content[0].text


async def test_extract_patterns_happy_path() -> None:
    payload = {
        "patterns": [
            {"pattern_id": "p2", "template": "GET <*> 200", "frequency": 10, "percentage": 20.0,
             "examples": ["GET /a 200"]},
            {"pattern_id": "p1", "template": "timeout after <*> ms", "frequency": 40,
             "percentage": 80.0, "examples": ["timeout after 30 ms"]},
        ],
        "statistics": {"total_logs_analyzed": 50},
    }  # fmt: skip
    deps, router = deps_for({("POST", f"{ORG}/streams/app/patterns/extract"): payload})
    out = await pt.extract_patterns_impl(deps, "app", "1h", 500)
    query = router.last["body"]["query"]
    assert query["sql"] == 'SELECT * FROM "app"' and query["size"] == 500
    assert out.data["patterns"][0]["template"] == "timeout after <*> ms"
    assert "example" not in out.data["patterns"][0]
    assert "| 40 | 80 | timeout after <*> ms |" in out.markdown


ALERT_ARGS = {
    "name": "api_errors",
    "stream": "default",
    "sql": "SELECT COUNT(*) AS n FROM \"default\" WHERE level = 'error' HAVING COUNT(*) > 5",
    "destinations": ["sink"],
}


async def test_create_alert_dry_run_by_default_sends_nothing() -> None:
    server, router = server_for(AGENT_OBS_ALLOW_WRITES="1")
    result = await server.call_tool("create_alert", ALERT_ARGS)
    assert not result.isError and router.requests == []
    request = result.structuredContent["request"]
    assert request["path"] == f"{V2}/alerts" and request["method"] == "POST"
    body = request["body"]
    assert body["query_condition"] == {
        "type": "sql",
        "sql": ALERT_ARGS["sql"],
        "conditions": None,
        "vrl_function": None,
    }
    assert body["trigger_condition"]["frequency_type"] == "minutes"
    assert body["destinations"] == ["sink"] and body["is_real_time"] is False
    assert "Nothing was sent" in result.content[0].text


async def test_create_alert_refused_without_allow_writes() -> None:
    server, router = server_for()
    result = await server.call_tool("create_alert", {**ALERT_ARGS, "dry_run": False})
    assert result.isError and "AGENT_OBS_ALLOW_WRITES=1" in result.content[0].text
    assert router.requests == []


async def test_create_alert_sends_with_both_gates_open() -> None:
    server, router = server_for(
        {("POST", f"{V2}/alerts"): {"code": 200, "message": "Alert saved"}},
        AGENT_OBS_ALLOW_WRITES="1",
    )
    args = {**ALERT_ARGS, "dry_run": False, "folder": "ops"}
    result = await server.call_tool("create_alert", args)
    assert not result.isError
    assert router.last["params"] == {"folder": "ops"}
    assert router.last["body"]["name"] == "api_errors"
    assert result.structuredContent["sent"] is True


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"name": "API errors!"}, "snake_case"),
        ({"destinations": [" "]}, "destination"),
        ({"sql": "DELETE FROM default"}, "SELECT"),
    ],
)
async def test_create_alert_validates_input(override: dict[str, Any], match: str) -> None:
    deps, _ = deps_for()
    spec = pt.AlertSpec(**{**ALERT_ARGS, **override})
    with pytest.raises(ValueError, match=match):
        await pt.create_alert_impl(deps, spec)


async def test_create_dashboard_gating() -> None:
    deps, router = deps_for(
        {("POST", f"{ORG}/dashboards"): {"v8": {"dashboardId": "new"}}},
        AGENT_OBS_ALLOW_WRITES="1",
    )
    dry = await pt.create_dashboard_impl(deps, "Ops", "desc")
    assert router.requests == [] and dry.data["dry_run"] is True
    assert dry.data["request"]["body"]["tabs"][0]["tabId"] == "default"
    assert dry.data["request"]["body"]["version"] == 8
    sent = await pt.create_dashboard_impl(deps, "Ops", dry_run=False, folder="f1")
    assert router.last["params"] == {"folder": "f1"} and sent.data["sent"] is True
    locked, locked_router = deps_for()
    with pytest.raises(ValueError, match="refused"):
        await pt.create_dashboard_impl(locked, "Ops", dry_run=False)
    assert locked_router.requests == []
    with pytest.raises(ValueError):
        await pt.create_dashboard_impl(locked, "  ")


async def test_tool_annotations() -> None:
    server, _ = server_for()
    tools = {t.name: t for t in await server.list_tools()}
    for name in ("create_alert", "create_dashboard"):
        assert tools[name].annotations.readOnlyHint is False
    for name in ("search_sql", "list_streams", "promql_range", "get_incident"):
        assert tools[name].annotations.readOnlyHint is True
    mirrored = ("StreamList", "SearchSQL", "GetLatestTraces", "PrometheusRangeQuery", "GetIncident")
    descriptions = " ".join(t.description or "" for t in tools.values())
    assert all(f"Mirrors {op}" in descriptions for op in mirrored)


async def test_output_is_capped() -> None:
    hits = [{"k": f"row-{i}-" + "x" * 100} for i in range(500)]
    server, _ = server_for({("POST", f"{ORG}/_search"): {"hits": hits}})
    result = await server.call_tool("search_sql", {"sql": "SELECT k FROM s", "limit": 500})
    assert len(result.content[0].text) <= 8000
    assert result.structuredContent["truncated"] is True
