"""Parity with the built-in MCP: streams, search, PromQL, traces, alerts, dashboards."""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from .client import O2Error
from .deps import Deps, clamp, require_id
from .render import ToolOutput, empty, fmt_num, fmt_us_ts, heading, table, to_float
from .sqlguard import guard_sql, is_content_column, strip_content
from .timewin import US_PER_S, parse_window, window_range
from .toolkit import READ_ONLY, WRITE, Window, run_tool

StreamKind = Literal["logs", "traces", "metrics"]
OutputFormat = Literal["csv", "md_table", "json"]
StreamName = Annotated[str, Field(description="Stream name, e.g. default or claude_code")]
DryRun = Annotated[
    bool, Field(description="True (default) only shows the request that would be sent")
]

MAX_CELL = 120
MAX_ROWS_SHOWN = 100
MAX_SERIES = 50
MAX_DAG_LINES = 300
UNAVAILABLE = "not available on this edition (enterprise feature) or not permitted for this user"
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")
_FIELD = re.compile(r"^[A-Za-z_@][A-Za-z0-9_.@-]{0,127}$")
_ALERT_NAME = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_KSUID = re.compile(r"^[A-Za-z0-9]{27}$")
_NUMERIC_HINT = re.compile(
    r"(duration|latency|_ms$|_us$|_ns$|_s$|count|size|bytes|tokens|cost|usd|status_code|_num$)",
    re.I,
)
_FILTER_KEYWORDS = re.compile(
    r"\b(select|union|insert|update|delete|drop|create|alter|truncate)\b", re.I
)


@dataclass(frozen=True)
class AlertSpec:
    """Inputs of create_alert, kept together so the payload builder stays small."""

    name: str
    stream: str
    sql: str
    destinations: list[str]
    stream_type: str = "logs"
    period_minutes: int = 10
    frequency_minutes: int = 10
    threshold: int = 1
    operator: str = ">="
    silence_minutes: int = 60
    description: str = ""


def register(mcp: FastMCP, deps: Deps) -> None:
    """Register this module's tools on `mcp`."""
    _register_streams(mcp, deps)
    _register_search(mcp, deps)
    _register_traces_promql(mcp, deps)
    _register_alerts_dashboards(mcp, deps)
    _register_writes(mcp, deps)


async def list_streams_impl(
    deps: Deps, stream_type: str = "all", keyword: str = "", limit: int = 100
) -> ToolOutput:
    params: dict[str, Any] = {}
    if stream_type != "all":
        params["type"] = _kind(stream_type)
    if keyword:
        params["keyword"] = keyword
    payload = _as_dict(await deps.client.request("GET", "streams", params=params))
    items = [_stream_row(s) for s in payload.get("list") or [] if isinstance(s, dict)]
    if not items:
        return empty("Streams", f"No {stream_type} streams found.")
    items.sort(key=lambda r: r["doc_num"], reverse=True)
    shown = items[: clamp(limit, 1, 500)]
    rows = [
        [r["name"], r["stream_type"], int(r["doc_num"]), r["storage_mb"], r["fields"]]
        for r in shown
    ]
    body = table(["stream", "type", "docs", "storage MB", "fields"], rows)
    title = heading(f"{len(items)} streams ({stream_type})", "sizes are uncompressed MB")
    return ToolOutput(f"{title}\n\n{body}", {"streams": shown, "total": len(items)})


async def stream_schema_impl(deps: Deps, stream: str, stream_type: str = "logs") -> ToolOutput:
    path = f"streams/{_seg(stream)}/schema"
    payload = _as_dict(await deps.client.request("GET", path, params={"type": _kind(stream_type)}))
    fields = [
        {"name": str(f.get("name")), "type": str(f.get("type"))}
        for f in payload.get("schema") or []
        if isinstance(f, dict)
    ]
    if not fields:
        return empty(f"Schema of {stream}", "The stream has no fields or does not exist.")
    uds = [str(f.get("name")) for f in payload.get("uds_schema") or [] if isinstance(f, dict)]
    notes = _schema_notes(fields, uds)
    title = heading(f"{stream} ({stream_type}): {len(fields)} fields")
    body = table(["field", "type"], [[f["name"], f["type"]] for f in fields])
    text = f"{title}\n\n" + "\n".join(f"- {n}" for n in notes) + f"\n\n{body}"
    data = {
        "stream": stream,
        "stream_type": stream_type,
        "fields": fields,
        "uds_fields": uds,
        "notes": notes,
    }
    return ToolOutput(text, data)


async def search_sql_impl(
    deps: Deps,
    sql: str,
    stream_type: str = "logs",
    window: str = "1h",
    limit: int = 100,
    output_format: str = "csv",
) -> ToolOutput:
    if output_format not in ("csv", "md_table", "json"):
        raise ValueError("output_format must be csv, md_table or json")
    allow_content = deps.settings.allow_content
    guarded = guard_sql(sql, lambda _name: True, allow_content, limit)
    rng = window_range(window, deps.clock())
    body: dict[str, Any] = {"query": _query(guarded.sql, rng.start_us, rng.end_us, guarded.limit)}
    if allow_content and output_format != "json":
        body["agent_options"] = {"output_format": output_format}
    params = {"type": _kind(stream_type)}
    payload = _as_dict(await deps.client.request("POST", "_search", params=params, body=body))
    return _render_search(payload, guarded.sql, stream_type, window, output_format, allow_content)


async def search_values_impl(
    deps: Deps,
    stream: str,
    fields: list[str],
    stream_type: str = "logs",
    window: str = "24h",
    size: int = 10,
) -> ToolOutput:
    if not fields:
        raise ValueError("fields must name at least one field")
    for name in fields:
        _check_field(name, deps.settings.allow_content)
    rng = window_range(window, deps.clock())
    params = {
        "type": _kind(stream_type),
        "fields": ",".join(fields),
        "size": clamp(size, 1, 100),
        "from": 0,
        "start_time": rng.start_us,
        "end_time": rng.end_us,
    }
    payload = _as_dict(await deps.client.request("GET", f"{_seg(stream)}/_values", params=params))
    groups = [_value_group(v) for v in payload.get("values") or [] if isinstance(v, dict)]
    groups = [g for g in groups if g["values"]]
    if not groups:
        return empty(f"Values in {stream}", f"No values for {', '.join(fields)} in {window}.")
    parts = [heading(f"Top values in {stream} (last {window})")]
    for g in groups:
        rows = [[v["value"], v["count"]] for v in g["values"]]
        parts.append(f"**{g['field']}**\n\n" + table(["value", "count"], rows))
    return ToolOutput("\n\n".join(parts), {"stream": stream, "window": window, "fields": groups})


async def search_around_impl(
    deps: Deps, stream: str, timestamp_us: int, size: int = 20, stream_type: str = "logs"
) -> ToolOutput:
    params = {"type": _kind(stream_type), "key": int(timestamp_us), "size": clamp(size, 2, 100)}
    payload = _as_dict(await deps.client.request("GET", f"{_seg(stream)}/_around", params=params))
    rows = _hits(payload)
    if not deps.settings.allow_content:
        rows = strip_content(rows)
    if not rows:
        return empty(f"Around {timestamp_us} in {stream}", "No records near that timestamp.")
    rows.sort(key=lambda r: to_float(r.get("_timestamp")) or 0)
    columns = _columns(rows)
    marked = [
        ["★" if r.get("_timestamp") == timestamp_us else "", *_cells(r, columns)] for r in rows
    ]
    title = heading(f"{len(rows)} records around {fmt_us_ts(timestamp_us)} UTC in {stream}")
    text = f"{title}\n\n" + table(["", *columns], marked)
    return ToolOutput(text, {"stream": stream, "key": timestamp_us, "rows": rows})


async def extract_patterns_impl(
    deps: Deps, stream: str, window: str = "1h", sample_size: int = 2000
) -> ToolOutput:
    rng = window_range(window, deps.clock())
    sql = f"SELECT * FROM {_quote(stream)}"
    body = {"query": _query(sql, rng.start_us, rng.end_us, clamp(sample_size, 100, 10_000))}
    path = f"streams/{_seg(stream)}/patterns/extract"
    payload = await _enterprise(deps, "POST", path, body=body)
    if payload is None:
        return _unavailable("Log patterns", f"ExtractPatterns is {UNAVAILABLE}.")
    patterns = [_pattern_row(p, deps.settings.allow_content) for p in _list(payload, "patterns")]
    if not patterns:
        return empty(f"Patterns in {stream}", f"No patterns extracted from the last {window}.")
    patterns.sort(key=lambda p: p["frequency"], reverse=True)
    rows = [[p["frequency"], p["percentage"], p["template"]] for p in patterns]
    stats = payload.get("statistics") if isinstance(payload, dict) else None
    title = heading(f"{len(patterns)} log patterns in {stream} (last {window})")
    text = f"{title}\n\n" + table(["count", "%", "template"], rows)
    return ToolOutput(text, {"stream": stream, "patterns": patterns, "statistics": stats})


async def latest_traces_impl(
    deps: Deps, stream: str = "default", window: str = "1h", limit: int = 20, filter: str = ""
) -> ToolOutput:
    rng = window_range(window, deps.clock())
    params: dict[str, Any] = {
        "from": 0,
        "size": clamp(limit, 1, 100),
        "start_time": rng.start_us,
        "end_time": rng.end_us,
        "sort_by": "duration",
        "sort_order": "desc",
    }
    if filter.strip():
        params["filter"] = _check_filter(filter)
    path = f"{_seg(stream)}/traces/latest"
    payload = _as_dict(await deps.client.request("GET", path, params=params))
    traces = [_trace_row(t, deps.settings.allow_content) for t in _hits(payload)]
    if not traces:
        return empty(f"Traces in {stream}", f"No traces in the last {window}.")
    rows = [
        [
            t["trace_id"],
            t["service"],
            t["operation"],
            t["duration_ms"],
            t["spans"],
            t["errors"],
            fmt_us_ts(t["start_us"]),
        ]
        for t in traces
    ]
    headers = ["trace_id", "root service", "operation", "ms", "spans", "errors", "start UTC"]
    title = heading(f"{len(traces)} slowest traces in {stream} (last {window})")
    return ToolOutput(f"{title}\n\n" + table(headers, rows), {"stream": stream, "traces": traces})


async def trace_dag_impl(deps: Deps, stream: str, trace_id: str, window: str = "7d") -> ToolOutput:
    require_id("trace_id", trace_id)
    rng = window_range(window, deps.clock())
    params = {"start_time": rng.start_us, "end_time": rng.end_us}
    path = f"{_seg(stream)}/traces/{quote(trace_id, safe='')}/dag"
    payload = _as_dict(await deps.client.request("GET", path, params=params))
    nodes = [_dag_node(n) for n in payload.get("nodes") or [] if isinstance(n, dict)]
    if not nodes:
        return empty(f"Trace {trace_id}", f"No spans found in the last {window}.")
    lines = _dag_lines(nodes, payload.get("edges") or [])
    errors = sum(1 for n in nodes if n["status"].upper() == "ERROR")
    title = heading(f"Trace {trace_id}: {len(nodes)} spans, {errors} errors")
    return ToolOutput(f"{title}\n\n" + "\n".join(lines), {"trace_id": trace_id, "nodes": nodes})


async def promql_query_impl(deps: Deps, query: str, time: str = "") -> ToolOutput:
    params = {"query": _check_promql(query)}
    if time.strip():
        params["time"] = time.strip()
    payload = await deps.client.request("GET", "prometheus/api/v1/query", params=params)
    kind, result = _prom_result(payload)
    series = [_instant_series(s) for s in result if isinstance(s, dict)]
    if kind in ("scalar", "string") and len(result) > 1:
        series = [{"labels": kind, "value": to_float(result[1])}]
    if not series:
        return empty("PromQL", f"No series for `{query}`.")
    rows = [[s["labels"], s["value"]] for s in series[:MAX_SERIES]]
    title = heading(f"{len(series)} series ({kind})", query)
    data = {"query": query, "result_type": kind, "series": series[:MAX_SERIES]}
    return ToolOutput(f"{title}\n\n" + table(["series", "value"], rows), data)


async def promql_range_impl(
    deps: Deps, query: str, window: str = "1h", step: str = ""
) -> ToolOutput:
    seconds = parse_window(window)
    end_s = int(deps.clock())
    params = {
        "query": _check_promql(query),
        "start": end_s - seconds,
        "end": end_s,
        "step": step.strip() or f"{max(15, seconds // 240)}s",
    }
    payload = await deps.client.request("GET", "prometheus/api/v1/query_range", params=params)
    kind, result = _prom_result(payload)
    series = [_range_series(s) for s in result if isinstance(s, dict)]
    if not series:
        return empty("PromQL range", f"No series for `{query}` in the last {window}.")
    series.sort(key=lambda s: s["max"] if s["max"] is not None else float("-inf"), reverse=True)
    shown = series[:MAX_SERIES]
    rows = [[s["labels"], s["points"], s["min"], s["avg"], s["max"], s["last"]] for s in shown]
    title = heading(f"{len(series)} series, last {window}, step {params['step']}", query)
    text = f"{title}\n\n" + table(["series", "points", "min", "avg", "max", "last"], rows)
    data = {"query": query, "result_type": kind, "step": params["step"], "series": shown}
    return ToolOutput(text, data)


async def list_alerts_impl(deps: Deps) -> ToolOutput:
    payload = _as_dict(await deps.client.request("GET", _v2(deps, "alerts")))
    alerts = [_alert_row(a) for a in payload.get("list") or [] if isinstance(a, dict)]
    if not alerts:
        return empty("Alerts", "No alerts are defined.")
    rows = [
        [
            a["name"],
            a["alert_type"],
            a["stream"],
            "yes" if a["enabled"] else "no",
            a["last_outcome"] or "–",
            fmt_us_ts(a["last_triggered_at"]),
            a["folder"],
        ]
        for a in alerts
    ]
    headers = ["alert", "type", "stream", "enabled", "last outcome", "last fired UTC", "folder"]
    text = f"{heading(f'{len(alerts)} alerts')}\n\n" + table(headers, rows)
    return ToolOutput(text, {"alerts": alerts})


async def alert_history_impl(
    deps: Deps, window: str = "24h", alert: str = "", limit: int = 200
) -> ToolOutput:
    rng = window_range(window, deps.clock())
    params: dict[str, Any] = {
        "start_time": rng.start_us,
        "end_time": rng.end_us,
        "from": 0,
        "size": clamp(limit, 1, 1000),
    }
    alert = alert.strip()
    if _KSUID.match(alert):
        params["alert_id"] = alert
    path = _v2(deps, "alerts/history")
    payload = _as_dict(await deps.client.request("GET", path, params=params))
    hits = [_history_row(h) for h in _hits(payload)]
    if alert and "alert_id" not in params:
        hits = [h for h in hits if h["alert_name"] == alert]
    if not hits:
        return empty("Alert history", f"No alert evaluations in the last {window}.")
    return _render_history(hits, window, payload.get("total"))


async def list_dashboards_impl(deps: Deps, folder: str = "", title: str = "") -> ToolOutput:
    params = {k: v for k, v in (("folder", folder), ("title", title)) if v}
    payload = _as_dict(await deps.client.request("GET", "dashboards", params=params))
    boards = [_dashboard_row(d) for d in payload.get("dashboards") or [] if isinstance(d, dict)]
    scope = f"folder {folder}" if folder else ("title search" if title else "default folder")
    if not boards:
        return empty("Dashboards", f"No dashboards in the {scope}.")
    rows = [[b["dashboard_id"], b["title"], b["folder"], b["owner"]] for b in boards]
    body = table(["id", "title", "folder", "owner"], rows)
    return ToolOutput(
        f"{heading(f'{len(boards)} dashboards ({scope})')}\n\n{body}", {"dashboards": boards}
    )


async def get_dashboard_impl(deps: Deps, dashboard_id: str) -> ToolOutput:
    payload = _as_dict(await deps.client.request("GET", f"dashboards/{_seg(dashboard_id)}"))
    board = payload.get(f"v{payload.get('version')}") or payload
    if not isinstance(board, dict):
        raise O2Error("OpenObserve returned an unexpected dashboard shape")
    panels = list(_panels(board))
    lines = [f"- [{p['tab']}] **{p['title']}** ({p['type']}) {p['query']}" for p in panels]
    title = heading(f"Dashboard: {board.get('title', dashboard_id)}", board.get("description", ""))
    text = f"{title}\n\n{len(panels)} panels\n\n" + "\n".join(lines)
    data = {"dashboard_id": dashboard_id, "title": board.get("title"), "panels": panels}
    return ToolOutput(text, data)


async def list_incidents_impl(deps: Deps, window: str = "7d", status: str = "") -> ToolOutput:
    rng = window_range(window, deps.clock())
    params: dict[str, Any] = {"limit": 100, "offset": 0}
    if status:
        params["status"] = status
    payload = await _enterprise(deps, "GET", _v2(deps, "alerts/incidents"), params=params)
    if payload is None:
        return _unavailable("Incidents", f"ListIncidents is {UNAVAILABLE}.")
    incidents = [_incident_row(i) for i in _list(payload, "incidents")]
    incidents = [i for i in incidents if (i["last_alert_us"] or 0) >= rng.start_us]
    if not incidents:
        hint = "On the open-source edition this endpoint always returns an empty list."
        return empty("Incidents", f"No incidents active in the last {window}. {hint}")
    rows = [
        [
            i["id"],
            i["status"],
            i["severity"],
            i["alert_count"],
            i["title"],
            fmt_us_ts(i["last_alert_us"]),
        ]
        for i in incidents
    ]
    headers = ["id", "status", "severity", "alerts", "title", "last alert UTC"]
    text = f"{heading(f'{len(incidents)} incidents (last {window})')}\n\n" + table(headers, rows)
    return ToolOutput(text, {"incidents": incidents})


async def get_incident_impl(deps: Deps, incident_id: str) -> ToolOutput:
    path = _v2(deps, f"alerts/incidents/{_seg(incident_id)}")
    payload = await _enterprise(deps, "GET", path)
    if not isinstance(payload, dict):
        reason = f"Incident {incident_id} was not found, or GetIncident is {UNAVAILABLE}."
        return _unavailable(f"Incident {incident_id}", reason)
    incident = _incident_row(payload)
    triggers = [_trigger_row(t) for t in payload.get("triggers") or [] if isinstance(t, dict)]
    head = heading(f"Incident {incident['id']}: {incident['title'] or '(untitled)'}")
    meta = (
        f"status **{incident['status']}**, severity **{incident['severity']}**, "
        f"{incident['alert_count']} alerts, {fmt_us_ts(incident['first_alert_us'])} → "
        f"{fmt_us_ts(incident['last_alert_us'])} UTC"
    )
    rows = [[t["alert_name"], fmt_us_ts(t["fired_us"]), t["reason"]] for t in triggers]
    text = f"{head}\n\n{meta}\n\n" + table(["alert", "fired UTC", "correlation"], rows)
    return ToolOutput(text, {"incident": incident, "triggers": triggers})


async def create_alert_impl(
    deps: Deps, spec: AlertSpec, folder: str = "", dry_run: bool = True
) -> ToolOutput:
    body = _alert_payload(spec)
    params = {"folder": folder} if folder else None
    return await _gated_write(deps, _v2(deps, "alerts"), params, body, dry_run, "alert")


async def create_dashboard_impl(
    deps: Deps,
    title: str,
    description: str = "",
    tabs: list[dict[str, Any]] | None = None,
    folder: str = "",
    dry_run: bool = True,
) -> ToolOutput:
    if not title.strip():
        raise ValueError("title must not be empty")
    if tabs is not None and not all(isinstance(t, dict) for t in tabs):
        raise ValueError("tabs must be a list of tab objects")
    body = {
        "version": 8,
        "title": title.strip(),
        "description": description,
        "tabs": tabs or [{"tabId": "default", "name": "Default", "panels": []}],
    }
    params = {"folder": folder} if folder else None
    path = f"/api/{deps.settings.org}/dashboards"
    return await _gated_write(deps, path, params, body, dry_run, "dashboard")


def _register_streams(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(annotations=READ_ONLY)
    async def list_streams(
        stream_type: Literal["logs", "traces", "metrics", "all"] = "all",
        keyword: str = "",
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> CallToolResult:
        """Streams with type, doc count and storage size, largest first. Mirrors StreamList."""
        return await run_tool(list_streams_impl(deps, stream_type, keyword, limit))

    @tool(annotations=READ_ONLY)
    async def stream_schema(stream: StreamName, stream_type: StreamKind = "logs") -> CallToolResult:
        """Fields and types of a stream plus SQL gotchas (Utf8 numerics). Mirrors StreamSchema."""
        return await run_tool(stream_schema_impl(deps, stream, stream_type))


def _register_search(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(annotations=READ_ONLY)
    async def search_sql(
        sql: Annotated[str, Field(description="One SELECT over any stream")],
        stream_type: StreamKind = "logs",
        window: Window = "1h",
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        output_format: OutputFormat = "csv",
    ) -> CallToolResult:
        """Guarded SQL over any stream: one SELECT, no DDL/DML, LIMIT ≤500, content columns
        blocked unless allowed. Mirrors SearchSQL."""
        return await run_tool(search_sql_impl(deps, sql, stream_type, window, limit, output_format))

    @tool(annotations=READ_ONLY)
    async def search_values(
        stream: StreamName,
        fields: Annotated[list[str], Field(min_length=1, max_length=10)],
        stream_type: StreamKind = "logs",
        window: Window = "24h",
        size: Annotated[int, Field(ge=1, le=100)] = 10,
    ) -> CallToolResult:
        """Top distinct values (with counts) of one or more fields. Mirrors SearchValues."""
        return await run_tool(search_values_impl(deps, stream, fields, stream_type, window, size))

    @tool(annotations=READ_ONLY)
    async def search_around(
        stream: StreamName,
        timestamp_us: Annotated[int, Field(description="_timestamp (µs) of the anchor record")],
        size: Annotated[int, Field(ge=2, le=100)] = 20,
        stream_type: StreamKind = "logs",
    ) -> CallToolResult:
        """Records just before and after one record, for context. Mirrors SearchAround."""
        return await run_tool(search_around_impl(deps, stream, timestamp_us, size, stream_type))

    @tool(annotations=READ_ONLY)
    async def extract_patterns(
        stream: StreamName,
        window: Window = "1h",
        sample_size: Annotated[int, Field(ge=100, le=10_000)] = 2000,
    ) -> CallToolResult:
        """Cluster log lines into templates by frequency (enterprise). Mirrors ExtractPatterns."""
        return await run_tool(extract_patterns_impl(deps, stream, window, sample_size))


def _register_traces_promql(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(annotations=READ_ONLY)
    async def latest_traces(
        stream: StreamName = "default",
        window: Window = "1h",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        filter: Annotated[str, Field(description="SQL condition, e.g. service_name='api'")] = "",
    ) -> CallToolResult:
        """Trace summaries (root service/operation, duration, spans, errors), slowest first.
        Mirrors GetLatestTraces."""
        return await run_tool(latest_traces_impl(deps, stream, window, limit, filter))

    @tool(annotations=READ_ONLY)
    async def trace_dag(
        trace_id: Annotated[str, Field(description="trace_id to expand")],
        stream: StreamName = "default",
        window: Window = "7d",
    ) -> CallToolResult:
        """Span DAG of one trace as an indented tree with service, operation, duration and
        errors. Mirrors GetTraceDAG."""
        return await run_tool(trace_dag_impl(deps, stream, trace_id, window))

    @tool(annotations=READ_ONLY)
    async def promql_query(
        query: Annotated[str, Field(description="PromQL expression")],
        time: Annotated[str, Field(description="RFC3339 or unix seconds; empty = now")] = "",
    ) -> CallToolResult:
        """Instant PromQL query; one row per series. Mirrors PrometheusQuery."""
        return await run_tool(promql_query_impl(deps, query, time))

    @tool(annotations=READ_ONLY)
    async def promql_range(
        query: Annotated[str, Field(description="PromQL expression")],
        window: Window = "1h",
        step: Annotated[str, Field(description="e.g. 60s or 5m; empty = automatic")] = "",
    ) -> CallToolResult:
        """Range PromQL query summarised per series (points, min/avg/max/last).
        Mirrors PrometheusRangeQuery."""
        return await run_tool(promql_range_impl(deps, query, window, step))


def _register_alerts_dashboards(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(annotations=READ_ONLY)
    async def list_alerts() -> CallToolResult:
        """Alerts with type, stream, enabled flag and last outcome. Mirrors ListAlerts."""
        return await run_tool(list_alerts_impl(deps))

    @tool(annotations=READ_ONLY)
    async def alert_history(
        window: Window = "24h",
        alert: Annotated[
            str, Field(description="Alert id (KSUID) or exact name; empty = all")
        ] = "",
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> CallToolResult:
        """Alert evaluations in the window with status counts per alert. Mirrors GetAlertHistory."""
        return await run_tool(alert_history_impl(deps, window, alert, limit))

    @tool(annotations=READ_ONLY)
    async def list_dashboards(folder: str = "", title: str = "") -> CallToolResult:
        """Dashboards in the default folder or a given folder/title. Mirrors ListDashboards."""
        return await run_tool(list_dashboards_impl(deps, folder, title))

    @tool(annotations=READ_ONLY)
    async def get_dashboard(dashboard_id: str) -> CallToolResult:
        """Dashboard summary: tabs, panels, panel types and queries. Mirrors GetDashboard."""
        return await run_tool(get_dashboard_impl(deps, dashboard_id))

    @tool(annotations=READ_ONLY)
    async def list_incidents(
        window: Window = "7d", status: Literal["", "open", "acknowledged", "resolved"] = ""
    ) -> CallToolResult:
        """Correlated alert incidents active in the window (enterprise). Mirrors ListIncidents."""
        return await run_tool(list_incidents_impl(deps, window, status))

    @tool(annotations=READ_ONLY)
    async def get_incident(incident_id: str) -> CallToolResult:
        """One incident with its correlated triggers (enterprise). Mirrors GetIncident."""
        return await run_tool(get_incident_impl(deps, incident_id))


def _register_writes(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(annotations=WRITE)
    async def create_alert(
        name: Annotated[str, Field(description="snake_case alert name")],
        stream: StreamName,
        sql: Annotated[str, Field(description="SELECT whose result rows trigger the alert")],
        destinations: Annotated[list[str], Field(min_length=1)],
        stream_type: StreamKind = "logs",
        period_minutes: Annotated[int, Field(ge=1, le=10_080)] = 10,
        frequency_minutes: Annotated[int, Field(ge=1, le=10_080)] = 10,
        threshold: Annotated[int, Field(ge=0)] = 1,
        operator: Literal["=", "!=", ">", ">=", "<", "<="] = ">=",
        silence_minutes: Annotated[int, Field(ge=0)] = 60,
        description: str = "",
        folder: str = "",
        dry_run: DryRun = True,
    ) -> CallToolResult:
        """Create a scheduled SQL alert. Dry run by default; sends only with dry_run=false AND
        AGENT_OBS_ALLOW_WRITES=1. Mirrors CreateAlert."""
        spec = AlertSpec(
            name=name,
            stream=stream,
            sql=sql,
            destinations=destinations,
            stream_type=stream_type,
            period_minutes=period_minutes,
            frequency_minutes=frequency_minutes,
            threshold=threshold,
            operator=operator,
            silence_minutes=silence_minutes,
            description=description,
        )
        return await run_tool(create_alert_impl(deps, spec, folder, dry_run))

    @tool(annotations=WRITE)
    async def create_dashboard(
        title: str,
        description: str = "",
        tabs: Annotated[
            list[dict[str, Any]] | None,
            Field(description="v8 tabs with panels; empty = one empty Default tab"),
        ] = None,
        folder: str = "",
        dry_run: DryRun = True,
    ) -> CallToolResult:
        """Create a dashboard. Dry run by default; sends only with dry_run=false AND
        AGENT_OBS_ALLOW_WRITES=1. Mirrors CreateDashboard."""
        return await run_tool(
            create_dashboard_impl(deps, title, description, tabs, folder, dry_run)
        )


def _v2(deps: Deps, suffix: str) -> str:
    return f"/api/v2/{deps.settings.org}/{suffix}"


def _seg(value: str) -> str:
    """Validate and URL-encode one path segment (stream, dashboard or incident id)."""
    return quote(require_id("name", value), safe="")


def _quote(name: str) -> str:
    return '"' + require_id("stream", name).replace('"', '""') + '"'


def _kind(stream_type: str) -> str:
    if stream_type not in ("logs", "traces", "metrics"):
        raise ValueError("stream_type must be logs, traces or metrics")
    return stream_type


def _query(sql: str, start_us: int, end_us: int, size: int) -> dict[str, Any]:
    return {"sql": sql, "start_time": start_us, "end_time": end_us, "from": 0, "size": size}


def _as_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise O2Error("OpenObserve returned an unexpected response shape")
    return payload


def _hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hits = payload.get("hits")
    return [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []


def _list(payload: Any, key: str) -> list[dict[str, Any]]:
    items = payload.get(key) if isinstance(payload, dict) else payload
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _status(exc: O2Error) -> int | None:
    match = _HTTP_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


async def _enterprise(
    deps: Deps,
    method: Literal["GET", "POST"],
    path: str,
    params: dict[str, Any] | None = None,
    body: Any = None,
) -> Any:
    """Call an enterprise-only endpoint; None when the edition or user cannot use it."""
    try:
        return await deps.client.request(method, path, params=params, body=body)
    except O2Error as exc:
        if _status(exc) in (403, 404):
            return None
        raise


def _unavailable(title: str, reason: str) -> ToolOutput:
    return ToolOutput(f"{heading(title)}\n\n{reason}", {"available": False, "reason": reason})


def _stream_row(stream: dict[str, Any]) -> dict[str, Any]:
    stats = stream.get("stats") if isinstance(stream.get("stats"), dict) else {}
    return {
        "name": str(stream.get("name")),
        "stream_type": str(stream.get("stream_type")),
        "doc_num": to_float(stats.get("doc_num")) or 0,
        "storage_mb": round(to_float(stats.get("storage_size")) or 0.0, 2),
        "compressed_mb": round(to_float(stats.get("compressed_size")) or 0.0, 2),
        "fields": stream.get("total_fields"),
        "doc_time_min_us": stats.get("doc_time_min"),
        "doc_time_max_us": stats.get("doc_time_max"),
    }


def _schema_notes(fields: list[dict[str, str]], uds: list[str]) -> list[str]:
    notes = ["`_timestamp` is Int64 microseconds since the epoch (UTC)."]
    numeric_utf8 = [
        f["name"] for f in fields if f["type"].lower() == "utf8" and _NUMERIC_HINT.search(f["name"])
    ]
    if numeric_utf8:
        sample = ", ".join(f"`{n}`" for n in numeric_utf8[:8])
        notes.append(
            f"Numbers stored as Utf8 ({sample}): use TRY_CAST(x AS DOUBLE) to compare, "
            "sort or aggregate them, otherwise ordering and SUM are wrong."
        )
    if uds:
        notes.append(f"User-defined schema is active: only {len(uds)} fields are queryable.")
    return notes


def _render_search(
    payload: dict[str, Any],
    sql: str,
    stream_type: str,
    window: str,
    output_format: str,
    allow_content: bool,
) -> ToolOutput:
    if isinstance(payload.get("data"), str):
        block, fmt, rows = payload["data"], str(payload.get("format") or output_format), []
    else:
        rows = _hits(payload) if allow_content else strip_content(_hits(payload))
        block, fmt = _format_rows(rows, output_format), output_format
    count = len(rows) or _line_count(block, fmt)
    title = heading(f"{count} rows ({stream_type}, last {window})", sql)
    if payload.get("function_error"):
        title += f"\n\n> {payload['function_error']}"
    data = {
        "sql": sql,
        "stream_type": stream_type,
        "window": window,
        "format": fmt,
        "total": payload.get("total"),
        "took_ms": payload.get("took"),
        "scan_size": payload.get("scan_size"),
        "rows": rows,
        "formatted": None if rows else block,
    }
    return ToolOutput(f"{title}\n\n{_fence(block, fmt)}", data)


def _format_rows(rows: list[dict[str, Any]], output_format: str) -> str:
    if not rows:
        return ""
    columns = _columns(rows)
    if output_format == "md_table":
        return table(columns, [_cells(r, columns) for r in rows])
    if output_format == "json":
        return "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(c)) for c in columns])
    return buf.getvalue().rstrip("\n")


def _fence(block: str, fmt: str) -> str:
    if not block:
        return "No rows."
    if fmt == "md_table":
        return block
    return f"```{'json' if fmt in ('json', 'ndjson') else fmt}\n{block}\n```"


def _line_count(block: str, fmt: str) -> int:
    lines = [ln for ln in block.splitlines() if ln.strip()]
    header = {"csv": 1, "md_table": 2}.get(fmt, 0)
    return max(0, len(lines) - header)


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows[:50]:
        seen.update(dict.fromkeys(row))
    return list(seen)[:20]


def _cells(row: dict[str, Any], columns: Iterable[str]) -> list[Any]:
    return [_clip(row.get(c)) for c in columns]


def _clip(value: Any) -> Any:
    if isinstance(value, dict | list):
        value = json.dumps(value, default=str)
    if isinstance(value, str) and len(value) > MAX_CELL:
        return value[:MAX_CELL] + "…"
    return value


def _csv_cell(value: Any) -> Any:
    value = _clip(value)
    return value.replace("\n", "\\n") if isinstance(value, str) else value


def _check_field(name: str, allow_content: bool) -> None:
    if not _FIELD.match(name or ""):
        raise ValueError(f"invalid field name {name!r}")
    if not allow_content and is_content_column(name):
        raise ValueError(
            f"field {name!r} holds prompt/tool content and is blocked; "
            "set AGENT_OBS_ALLOW_CONTENT=1 to allow it"
        )


def _value_group(group: dict[str, Any]) -> dict[str, Any]:
    values = [
        {"value": v.get("zo_sql_key"), "count": v.get("zo_sql_num")}
        if isinstance(v, dict)
        else {"value": v, "count": None}
        for v in group.get("values") or []
    ]
    return {"field": str(group.get("field")), "values": values}


def _pattern_row(pattern: dict[str, Any], allow_content: bool) -> dict[str, Any]:
    row = {
        "pattern_id": pattern.get("pattern_id"),
        "template": _clip(str(pattern.get("template", ""))),
        "frequency": to_float(pattern.get("frequency")) or 0,
        "percentage": to_float(pattern.get("percentage")),
    }
    examples = pattern.get("examples")
    if allow_content and isinstance(examples, list) and examples:
        row["example"] = _clip(examples[0])
    return row


def _check_filter(text: str) -> str:
    """The server pastes the filter into its own WHERE clause, so allow one plain condition."""
    if ";" in text or "--" in text or "/*" in text:
        raise ValueError("filter must be a single condition without ';' or comments")
    if _FILTER_KEYWORDS.search(text):
        raise ValueError("filter must be a plain condition such as service_name='api'")
    return text.strip()


def _ts_us(value: Any) -> int | None:
    """Normalise a s/ms/µs/ns epoch timestamp to microseconds."""
    num = to_float(value)
    if num is None or num <= 0:
        return None
    if num >= 1e17:
        return int(num / 1000)
    if num >= 1e14:
        return int(num)
    if num >= 1e11:
        return int(num * 1000)
    return int(num * US_PER_S)


def _trace_row(trace: dict[str, Any], allow_content: bool) -> dict[str, Any]:
    first = trace.get("first_event") if isinstance(trace.get("first_event"), dict) else {}
    spans = trace.get("spans") if isinstance(trace.get("spans"), list) else []
    services = [s.get("service_name") for s in _list(trace, "service_name")]
    row = {
        "trace_id": trace.get("trace_id"),
        "service": first.get("service_name"),
        "operation": first.get("operation_name"),
        "duration_ms": round((to_float(trace.get("duration")) or 0) / 1000, 2),
        "spans": spans[0] if spans else None,
        "errors": spans[1] if len(spans) > 1 else None,
        "start_us": _ts_us(trace.get("start_time")),
        "services": services,
        "models": trace.get("models") or [],
        "total_tokens": trace.get("gen_ai_usage_total_tokens"),
    }
    if allow_content and trace.get("gen_ai_input_messages") is not None:
        row["gen_ai_input_messages"] = _clip(trace["gen_ai_input_messages"])
    return row


def _dag_node(node: dict[str, Any]) -> dict[str, Any]:
    start, end = to_float(node.get("start_time")), to_float(node.get("end_time"))
    duration_ms = (end - start) / 1_000_000 if start is not None and end is not None else None
    return {
        "span_id": str(node.get("span_id")),
        "parent_span_id": node.get("parent_span_id") or None,
        "service": node.get("service_name") or "?",
        "operation": node.get("operation_name") or "?",
        "status": str(node.get("span_status") or "UNSET"),
        "start_ns": start or 0,
        "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
    }


def _dag_children(nodes: dict[str, dict[str, Any]], edges: list[Any]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for edge in edges:
        if isinstance(edge, dict) and edge.get("from") in nodes and edge.get("to") in nodes:
            children.setdefault(str(edge["from"]), []).append(str(edge["to"]))
    return children


def _dag_lines(nodes: list[dict[str, Any]], edges: list[Any]) -> list[str]:
    by_id = {n["span_id"]: n for n in nodes}
    children = _dag_children(by_id, edges)
    linked = {c for kids in children.values() for c in kids}
    roots = sorted((n for n in nodes if n["span_id"] not in linked), key=lambda n: n["start_ns"])
    lines: list[str] = []
    stack = [(0, n["span_id"]) for n in reversed(roots)]
    seen: set[str] = set()
    while stack and len(lines) < MAX_DAG_LINES:
        depth, span_id = stack.pop()
        if span_id in seen:
            continue
        seen.add(span_id)
        lines.append("  " * depth + _dag_label(by_id[span_id]))
        kids = sorted(children.get(span_id, []), key=lambda k: by_id[k]["start_ns"], reverse=True)
        stack.extend((depth + 1, k) for k in kids)
    return lines


def _dag_label(node: dict[str, Any]) -> str:
    mark = " **ERROR**" if node["status"].upper() == "ERROR" else ""
    took = f"{fmt_num(node['duration_ms'])} ms" if node["duration_ms"] is not None else "? ms"
    return f"- {node['service']}: {node['operation']} — {took}{mark}"


def _check_promql(query: str) -> str:
    text = (query or "").strip()
    if not text:
        raise ValueError("query must not be empty")
    if len(text) > 4000:
        raise ValueError("query is too long (max 4000 characters)")
    return text


def _prom_result(payload: Any) -> tuple[str, list[Any]]:
    payload = _as_dict(payload)
    if payload.get("status") not in (None, "success"):
        raise O2Error(f"PromQL failed: {str(payload.get('error') or payload)[:300]}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result = data.get("result")
    return str(data.get("resultType") or "unknown"), result if isinstance(result, list) else []


def _labels(metric: Any) -> str:
    if not isinstance(metric, dict):
        return ""
    name = str(metric.get("__name__", ""))
    rest = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}" if rest else name or "{}"


def _instant_series(series: dict[str, Any]) -> dict[str, Any]:
    value = series.get("value")
    num = to_float(value[1]) if isinstance(value, list) and len(value) > 1 else None
    return {"labels": _labels(series.get("metric")), "value": num}


def _range_series(series: dict[str, Any]) -> dict[str, Any]:
    raw = series.get("values") if isinstance(series.get("values"), list) else []
    parsed = (to_float(p[1]) for p in raw if isinstance(p, list) and len(p) > 1)
    nums = [v for v in parsed if v is not None]
    return {
        "labels": _labels(series.get("metric")),
        "points": len(nums),
        "min": min(nums) if nums else None,
        "avg": sum(nums) / len(nums) if nums else None,
        "max": max(nums) if nums else None,
        "last": nums[-1] if nums else None,
    }


def _alert_row(alert: dict[str, Any]) -> dict[str, Any]:
    return {
        "alert_id": alert.get("alert_id"),
        "name": alert.get("name"),
        "alert_type": alert.get("alert_type"),
        "stream": alert.get("stream_name") or "–",
        "stream_type": alert.get("stream_type"),
        "enabled": bool(alert.get("enabled")),
        "is_real_time": bool(alert.get("is_real_time")),
        "last_outcome": alert.get("last_outcome"),
        "last_triggered_at": alert.get("last_triggered_at"),
        "folder": alert.get("folder_name") or alert.get("folder_id"),
    }


def _history_row(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": hit.get("timestamp"),
        "alert_name": hit.get("alert_name"),
        "status": hit.get("status"),
        "retries": hit.get("retries"),
        "error": _clip(hit.get("error")) if hit.get("error") else None,
        "evaluation_took_s": hit.get("evaluation_took_in_secs"),
        "is_silenced": hit.get("is_silenced"),
    }


def _render_history(hits: list[dict[str, Any]], window: str, total: Any) -> ToolOutput:
    counts = Counter((str(h["alert_name"]), str(h["status"])) for h in hits)
    summary = [{"alert": a, "status": s, "count": n} for (a, s), n in counts.most_common()]
    recent = [
        [fmt_us_ts(h["timestamp"]), h["alert_name"], h["status"], h["error"] or ""]
        for h in hits[:MAX_ROWS_SHOWN]
    ]
    title = heading(f"{len(hits)} alert evaluations (last {window})", f"server total: {total}")
    text = (
        f"{title}\n\n"
        + table(["alert", "status", "count"], [list(s.values()) for s in summary])
        + "\n\n**Most recent**\n\n"
        + table(["time UTC", "alert", "status", "error"], recent)
    )
    return ToolOutput(text, {"summary": summary, "history": hits})


def _dashboard_row(board: dict[str, Any]) -> dict[str, Any]:
    return {
        "dashboard_id": board.get("dashboard_id"),
        "title": board.get("title"),
        "description": board.get("description"),
        "folder": board.get("folder_name") or board.get("folder_id"),
        "folder_id": board.get("folder_id"),
        "owner": board.get("owner"),
    }


def _panels(board: dict[str, Any]) -> Iterable[dict[str, Any]]:
    tabs = board.get("tabs")
    if not isinstance(tabs, list):
        tabs = [{"name": "-", "panels": board.get("panels") or []}]
    for tab in tabs:
        if isinstance(tab, dict):
            for panel in _list(tab, "panels"):
                yield _panel_row(str(tab.get("name", "-")), panel)


def _panel_row(tab: str, panel: dict[str, Any]) -> dict[str, Any]:
    queries = _list(panel, "queries")
    texts = [str(q.get("query") or "").strip() for q in queries]
    streams = [q["fields"].get("stream") for q in queries if isinstance(q.get("fields"), dict)]
    return {
        "tab": tab,
        "id": panel.get("id"),
        "title": panel.get("title"),
        "type": panel.get("type"),
        "query_type": panel.get("queryType"),
        "streams": [s for s in streams if s],
        "queries": texts,
        "query": f"`{_clip(texts[0])}`" if texts and texts[0] else "",
    }


def _incident_row(incident: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": incident.get("id"),
        "status": incident.get("status"),
        "severity": incident.get("severity"),
        "title": incident.get("title"),
        "alert_count": incident.get("alert_count"),
        "first_alert_us": _ts_us(incident.get("first_alert_at")),
        "last_alert_us": _ts_us(incident.get("last_alert_at")),
        "assigned_to": incident.get("assigned_to"),
    }


def _trigger_row(trigger: dict[str, Any]) -> dict[str, Any]:
    reason = trigger.get("correlation_reason")
    return {
        "alert_id": trigger.get("alert_id"),
        "alert_name": trigger.get("alert_name"),
        "fired_us": _ts_us(trigger.get("alert_fired_at")),
        "reason": json.dumps(reason) if isinstance(reason, dict | list) else reason,
    }


def _alert_payload(spec: AlertSpec) -> dict[str, Any]:
    if not _ALERT_NAME.match(spec.name or ""):
        raise ValueError("name must be snake_case: letters, digits and '_' only")
    if not spec.destinations or not all(d.strip() for d in spec.destinations):
        raise ValueError("at least one destination name is required")
    guard_sql(spec.sql, lambda _name: True, True)
    return {
        "name": spec.name,
        "description": spec.description,
        "stream_type": _kind(spec.stream_type),
        "stream_name": require_id("stream", spec.stream),
        "is_real_time": False,
        "query_condition": {
            "type": "sql",
            "sql": spec.sql.strip(),
            "conditions": None,
            "vrl_function": None,
        },
        "trigger_condition": {
            "period": spec.period_minutes,
            "operator": spec.operator,
            "threshold": spec.threshold,
            "frequency": spec.frequency_minutes,
            "frequency_type": "minutes",
            "silence": spec.silence_minutes,
        },
        "destinations": [d.strip() for d in spec.destinations],
        "enabled": True,
    }


async def _gated_write(
    deps: Deps,
    path: str,
    params: dict[str, Any] | None,
    body: dict[str, Any],
    dry_run: bool,
    what: str,
) -> ToolOutput:
    """Writes are dry runs unless the caller opts out AND the operator enabled writes."""
    request = {"method": "POST", "path": path, "params": params or {}, "body": body}
    if dry_run:
        shown = json.dumps(request, indent=2, ensure_ascii=False)
        text = (
            f"{heading(f'Dry run: create {what}')}\n\nNothing was sent. To send it, call again "
            f"with dry_run=false (the server needs AGENT_OBS_ALLOW_WRITES=1):\n\n"
            f"```json\n{shown}\n```"
        )
        return ToolOutput(text, {"dry_run": True, "sent": False, "request": request})
    if not deps.settings.allow_writes:
        raise ValueError(
            f"refused to create {what}: writes are disabled on this server. "
            "The operator must set AGENT_OBS_ALLOW_WRITES=1; use dry_run=true to preview."
        )
    response = await deps.client.request("POST", path, params=params, body=body)
    text = f"{heading(f'Created {what}')}\n\n```json\n{json.dumps(response, default=str)}\n```"
    data = {"dry_run": False, "sent": True, "request": request, "response": response}
    return ToolOutput(text, data)
