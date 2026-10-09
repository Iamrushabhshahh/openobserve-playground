"""SRE tools for agents: SLOs, regressions, incidents, budgets, alerts, telemetry health."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from . import traces as tr
from .claude_tools import FAILED, resolve_parent_tools
from .client import O2Error, StreamType
from .deps import Deps, in_list, quote_ident, sql_str
from .render import ToolOutput, empty, fmt_num, heading, num, table, to_float
from .timewin import US_PER_S, TimeRange, WindowError, parse_window, window_range
from .toolkit import READ_ONLY, Window, run_tool

RAW_CAP = 20_000
MIN_US = 60 * US_PER_S
DAY_S = 86_400
VERSION_COL = "service_service_version"
API_ERROR = "api_error"
RETRIES_EXHAUSTED = "api_retries_exhausted"
API_FAIL_EVENTS = (API_ERROR, RETRIES_EXHAUSTED)
REDACTED = "<REDACTED>"

# (long window s, short window s, burn-rate threshold, action): Google SRE workbook, chapter 5.
BURN_RULES: tuple[tuple[int, int, float, str], ...] = (
    (3600, 300, 14.4, "page"),
    (6 * 3600, 1800, 6.0, "page"),
    (24 * 3600, 2 * 3600, 3.0, "ticket"),
    (72 * 3600, 6 * 3600, 1.0, "ticket"),
)
LONGEST_RULE_S = max(rule[0] for rule in BURN_RULES)

REGRESSION_METRICS = ("ttft", "cost_per_interaction", "tool_failure_rate", "interaction_latency")
METRIC_UNITS = {
    "ttft": "ms",
    "cost_per_interaction": "USD",
    "tool_failure_rate": "%",
    "interaction_latency": "s",
}
Z_ALERT = 3.0
MIN_CHANGE_PCT = 10.0
MIN_RATE_CHANGE_PP = 1.0
MIN_SAMPLES = 10
MIN_GROUP = 5
NEW_SHARE = 0.05
MIX_SHIFT = 0.3
MAD_TO_SIGMA = 1.4826
MEDIAN_SE = 1.2533

INCIDENT_MAX_S = 7 * DAY_S
INCIDENT_CAP = 2000
SPIKE_FACTOR = 2.0
SPIKE_MIN_CALLS = 3
COLLAPSE_GAP_US = 5 * MIN_US
TIMELINE_ROWS = 40

TOP_SPENDERS = 5
ALERT_DESTINATION = "playground_alert_sink"
ALERT_TAGS = ("claude-code", "sre-draft")
MIN_ALERT_VOLUME = 10
STALE_MIN = 30
DROP_RATIO = 0.3
DROP_MIN_HOURLY = 5.0
MAX_HEALTH_STREAMS = 15
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 32.0
RATE_LIMIT_CODES = ("429", "529")


@dataclass(frozen=True)
class SliPoint:
    ts_us: int
    total: int
    bad: int


@dataclass(frozen=True)
class Sample:
    ts_us: int
    value: float
    version: str
    model: str


@dataclass(frozen=True)
class TimelineEvent:
    ts_us: int
    kind: str
    detail: str
    session_id: str = ""
    trace_id: str = ""


@dataclass(frozen=True)
class StreamRef:
    name: str
    stream_type: str
    stats_last_us: int | None = None


# ---------------------------------------------------------------- registration


def register(mcp: FastMCP, deps: Deps) -> None:
    """Register this module's tools on `mcp`."""
    _register_reliability(mcp, deps)
    _register_operations(mcp, deps)


def _register_reliability(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(
        name="agent_slo_report",
        annotations=READ_ONLY,
        description=(
            "SLIs for the agent as a service (interaction success, latency quantile, LLM "
            "availability) with error budget left, multi-window burn rates (14.4×/1h, 6×/6h, "
            "3×/24h, 1×/72h) and a verdict."
        ),
    )
    async def slo_tool(
        window: Window = "7d",
        success_target: Annotated[float, Field(gt=0.5, lt=1)] = 0.95,
        latency_target_s: Annotated[float, Field(gt=0, le=86_400)] = 300,
        latency_quantile: Annotated[float, Field(ge=0.5, lt=1)] = 0.95,
    ) -> CallToolResult:
        return await run_tool(
            agent_slo_report(deps, window, success_target, latency_target_s, latency_quantile)
        )

    @tool(
        name="regression_check",
        annotations=READ_ONLY,
        description=(
            "Recent vs baseline (median/p95, % change, MAD-based robust z) broken down by "
            "Claude Code version and model, so an agent upgrade or model switch shows up as "
            "the likely cause."
        ),
    )
    async def regression_tool(
        metric: Literal[
            "ttft", "cost_per_interaction", "tool_failure_rate", "interaction_latency"
        ] = "ttft",
        baseline: Window = "7d",
        recent: Window = "24h",
    ) -> CallToolResult:
        return await run_tool(regression_check(deps, metric, baseline, recent))

    @tool(
        name="incident_timeline",
        annotations=READ_ONLY,
        description=(
            "Blameless postmortem draft: API errors, exhausted retries, failed tools, MCP "
            "failures and TTFT spikes merged into one timeline with first/last bad event and "
            "affected sessions. No content."
        ),
    )
    async def incident_tool(
        start: Annotated[
            str | None, Field(description="ISO 8601 start (local zone if naive) or epoch s")
        ] = None,
        end: Annotated[str | None, Field(description="ISO 8601 end; default start+window")] = None,
        window: Window = "2h",
    ) -> CallToolResult:
        return await run_tool(incident_timeline(deps, start, end, window))

    @tool(
        name="rate_limit_report",
        annotations=READ_ONLY,
        description=(
            "429/529/overloaded API errors by hour and model, exhausted retries and estimated"
            " time lost to retries."
        ),
    )
    async def rate_limit_tool(window: Window = "7d") -> CallToolResult:
        return await run_tool(rate_limit_report(deps, window))


def _register_operations(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(
        name="budget_forecast",
        annotations=READ_ONLY,
        description=(
            "FinOps for the agent: month-to-date spend, trailing daily run-rate, projected "
            "month end, budget exhaustion date, cost per successful interaction, top "
            "traces/sessions and a cap recommendation. Context: Uber capped AI-coding spend "
            "per engineer per tool after burning a year's budget in four months."
        ),
    )
    async def budget_tool(
        monthly_budget_usd: Annotated[float, Field(gt=0, description="Monthly budget in USD")],
        window: Window = "7d",
    ) -> CallToolResult:
        return await run_tool(budget_forecast(deps, monthly_budget_usd, window))

    @tool(
        name="recommend_alerts",
        annotations=READ_ONLY,
        description=(
            "Alert thresholds derived from observed baselines (cost, API error rate, TTFT, "
            "tool failures, permission wait, MCP failures) as ready-to-import OpenObserve "
            "alert JSON drafts with rationale and backtested noise. Creates nothing."
        ),
    )
    async def alerts_tool(window: Window = "14d") -> CallToolResult:
        return await run_tool(recommend_alerts(deps, window))

    @tool(
        name="telemetry_health",
        annotations=READ_ONLY,
        description=(
            "Observability of the observability: per-stream last event age, 1h vs 24h volume,"
            " stale/drop flags, logs-without-traces and a compliance warning when prompt/tool"
            " content is being captured (counts only)."
        ),
    )
    async def health_tool() -> CallToolResult:
        return await run_tool(telemetry_health(deps))


# ---------------------------------------------------------------- shared helpers


def to_us(value: Any) -> int | None:
    """Epoch s/ms/µs/ns numbers or ISO strings (histogram buckets; naive means UTC) to µs."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if to_float(text) is None:
            try:
                when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.UTC)
            return int(when.timestamp() * US_PER_S)
    number = to_float(value)
    if number is None:
        return None
    for limit, scale in ((1e17, 1e-3), (1e14, 1.0), (1e11, 1e3)):
        if abs(number) >= limit:
            return int(number * scale)
    return int(number * US_PER_S)


def quantile(values: Iterable[float], q: float) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mad(values: Sequence[float]) -> float | None:
    med = quantile(values, 0.5)
    if med is None:
        return None
    return quantile([abs(v - med) for v in values], 0.5)


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return round(100 * (new - old) / abs(old), 1)


def dur_label(seconds: int) -> str:
    for unit, size in (("d", DAY_S), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def fmt_local(us: int | None, tz: dt.tzinfo, seconds: bool = False) -> str:
    if us is None:
        return "–"
    fmt = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    return dt.datetime.fromtimestamp(us / US_PER_S, tz).strftime(fmt)


def series(rows: Iterable[dict[str, Any]], key: str, ts_key: str = "t") -> list[tuple[int, float]]:
    out = []
    for row in rows:
        ts = to_us(row.get(ts_key))
        if ts is not None:
            out.append((ts, num(row.get(key))))
    return sorted(out)


def fold_daily(points: Iterable[tuple[int, float]], tz: dt.tzinfo) -> dict[str, float]:
    """Fold UTC buckets into local calendar days."""
    days: dict[str, float] = {}
    for ts, value in points:
        day = dt.datetime.fromtimestamp(ts / US_PER_S, tz).date().isoformat()
        days[day] = days.get(day, 0.0) + value
    return dict(sorted(days.items()))


def _now_us(deps: Deps) -> int:
    return int(deps.clock() * US_PER_S)


def _range_back(deps: Deps, seconds: int, end_us: int | None = None) -> TimeRange:
    end = _now_us(deps) if end_us is None else end_us
    return TimeRange(end - seconds * US_PER_S, end)


def _first_row(rows: list[dict[str, Any]], key: str) -> float:
    return num(rows[0].get(key)) if rows else 0.0


async def _first_ok(
    deps: Deps, variants: Sequence[str], stream_type: StreamType, rng: TimeRange, size: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Run the first SQL variant that succeeds; later variants avoid optional columns."""
    error: str | None = None
    for i, sql in enumerate(variants):
        rows, error = await deps.query_optional(sql, stream_type, rng, size)
        if error is None:
            return rows, None if i == 0 else "an optional column is missing; used a simpler query"
    return [], error


def _ids(rows: Iterable[dict[str, Any]], key: str = "trace_id") -> set[str]:
    return {str(r[key]) for r in rows if r.get(key)}


# ---------------------------------------------------------------- 1. SLO report


def burn_rate(bad: int, total: int, target: float) -> float | None:
    """Observed bad ratio divided by the allowed bad ratio (1 - target)."""
    if total <= 0:
        return None
    return (bad / total) / (1 - target)


def budget_remaining_pct(bad: int, total: int, target: float) -> float | None:
    rate = burn_rate(bad, total, target)
    return None if rate is None else round(100 * (1 - rate), 1)


def window_totals(points: Iterable[SliPoint], now_us: int, seconds: int) -> tuple[int, int]:
    start = now_us - seconds * US_PER_S
    sel = [p for p in points if start <= p.ts_us <= now_us]
    return sum(p.bad for p in sel), sum(p.total for p in sel)


def evaluate_burn(points: Sequence[SliPoint], now_us: int, target: float) -> list[dict[str, Any]]:
    """Multi-window multi-burn-rate: a rule fires only when the long AND short windows burn."""
    out = []
    for long_s, short_s, threshold, action in BURN_RULES:
        long_bad, long_total = window_totals(points, now_us, long_s)
        long_rate = burn_rate(long_bad, long_total, target)
        short_rate = burn_rate(*window_totals(points, now_us, short_s), target)
        fired = (
            long_rate is not None
            and short_rate is not None
            and long_rate >= threshold
            and short_rate >= threshold
        )
        out.append(
            {
                "long": dur_label(long_s),
                "short": dur_label(short_s),
                "threshold": threshold,
                "action": action,
                "long_burn": None if long_rate is None else round(long_rate, 2),
                "short_burn": None if short_rate is None else round(short_rate, 2),
                "events_long": long_total,
                "fired": fired,
            }
        )
    return out


def classify_interactions(
    rows: Iterable[dict[str, Any]],
    failed_traces: set[str],
    api_error_traces: set[str],
    latency_target_us: float,
) -> tuple[list[SliPoint], dict[str, int]]:
    """One SLI point per interaction; bad if a tool failed, the API errored, or it was too slow."""
    points: list[SliPoint] = []
    reasons = {"failed_tool": 0, "api_error": 0, "slow": 0}
    for row in rows:
        ts = to_us(row.get("_timestamp"))
        if ts is None:
            continue
        tid = str(row.get("trace_id") or "")
        flags = {
            "failed_tool": tid in failed_traces,
            "api_error": tid in api_error_traces,
            "slow": num(row.get("duration")) > latency_target_us,
        }
        for key, hit in flags.items():
            reasons[key] += int(hit)
        points.append(SliPoint(ts, 1, int(any(flags.values()))))
    return points, reasons


def availability_points(rows: Iterable[dict[str, Any]]) -> list[SliPoint]:
    out = []
    for row in rows:
        ts = to_us(row.get("t"))
        ok, errors = int(num(row.get("ok"))), int(num(row.get("errors")))
        if ts is not None and ok + errors > 0:
            out.append(SliPoint(ts, ok + errors, errors))
    return out


def slo_verdict(
    burns: dict[str, list[dict[str, Any]]],
    budgets: dict[str, float | None],
    latency_ok: bool | None,
) -> str:
    for action, label in (
        ("page", "PAGE — fast error-budget burn"),
        ("ticket", "TICKET — slow burn"),
    ):
        hits = [
            f"{sli} {r['long']}+{r['short']} at {r['long_burn']}× (≥{r['threshold']}×)"
            for sli, rules in burns.items()
            for r in rules
            if r["fired"] and r["action"] == action
        ]
        if hits:
            return f"{label}: " + "; ".join(hits)
    known = {k: v for k, v in budgets.items() if v is not None}
    if not known and latency_ok is None:
        return "NO DATA — nothing to evaluate in this window"
    exhausted = [k for k, v in known.items() if v < 0]
    if exhausted:
        return "SLO MISSED — error budget exhausted for " + ", ".join(exhausted)
    if latency_ok is False:
        return "SLO MISSED — interaction latency quantile is above target"
    if not known:
        return "OK — latency within target; no success/availability events"
    name, left = min(known.items(), key=lambda kv: kv[1])
    return f"OK — within SLO, no burn-rate rule firing; lowest budget left: {name} {left}%"


def _check_slo_args(target: float, latency_s: float, q: float) -> None:
    if not 0.5 < target < 1:
        raise ValueError("success_target must be between 0.5 and 1 (exclusive), e.g. 0.95")
    if not 0 < latency_s <= DAY_S:
        raise ValueError("latency_target_s must be between 0 and 86400")
    if not 0.5 <= q < 1:
        raise ValueError("latency_quantile must be in [0.5, 1), e.g. 0.95")


async def _slo_inputs(deps: Deps, rng: TimeRange) -> dict[str, Any]:
    c = deps.claude
    inter_sql = (
        f"SELECT trace_id, _timestamp, duration FROM {c} "
        f"WHERE operation_name = {sql_str(tr.OP_INTERACTION)} "
        f"ORDER BY _timestamp DESC LIMIT {RAW_CAP}"
    )
    failed_sql = (
        f"SELECT trace_id, COUNT(*) AS n FROM {c} WHERE operation_name = {sql_str(tr.OP_EXEC)} "
        f"AND {FAILED} GROUP BY trace_id LIMIT {RAW_CAP}"
    )
    api_sql = (
        f"SELECT trace_id, COUNT(*) AS n FROM {c} WHERE event_name IN ({in_list(API_FAIL_EVENTS)}) "
        f"AND trace_id IS NOT NULL GROUP BY trace_id LIMIT {RAW_CAP}"
    )
    avail_sql = (
        "SELECT histogram(_timestamp, '5 minute') AS t, "
        "SUM(CASE WHEN event_name = 'api_request' THEN 1 ELSE 0 END) AS ok, "
        f"SUM(CASE WHEN event_name = {sql_str(API_ERROR)} THEN 1 ELSE 0 END) AS errors "
        f"FROM {c} WHERE event_name IN ('api_request', {sql_str(API_ERROR)}) "
        "GROUP BY t ORDER BY t LIMIT 5000"
    )
    (inter, n1), (failed, n2), (api, n3), (avail, n4) = await asyncio.gather(
        deps.query_optional(inter_sql, "traces", rng, RAW_CAP),
        deps.query_optional(failed_sql, "traces", rng, RAW_CAP),
        deps.query_optional(api_sql, "logs", rng, RAW_CAP),
        deps.query_optional(avail_sql, "logs", rng, 5000),
    )
    notes = [n for n in (n1, n2, n3, n4) if n]
    if len(inter) >= RAW_CAP:
        notes.append(f"interaction SLI uses the latest {RAW_CAP} interactions only")
    if inter and not api and not n3:
        notes.append("no API error could be tied to a trace_id (logs may lack trace_id)")
    return {
        "inter": inter,
        "failed": _ids(failed),
        "api": _ids(api),
        "avail": avail,
        "notes": notes,
    }


async def agent_slo_report(
    deps: Deps,
    window: str = "7d",
    success_target: float = 0.95,
    latency_target_s: float = 300,
    latency_quantile: float = 0.95,
) -> ToolOutput:
    _check_slo_args(success_target, latency_target_s, latency_quantile)
    report = window_range(window, deps.clock())
    now_us = report.end_us
    fetch = TimeRange(min(report.start_us, now_us - LONGEST_RULE_S * US_PER_S), now_us)
    data = await _slo_inputs(deps, fetch)
    if not data["inter"] and not data["avail"]:
        return empty("Agent SLO report", f"No interactions or API calls in the last {window}.")
    points, reasons = classify_interactions(
        data["inter"], data["failed"], data["api"], latency_target_s * US_PER_S
    )
    durations = [
        num(r.get("duration")) / US_PER_S
        for r in data["inter"]
        if (to_us(r.get("_timestamp")) or 0) >= report.start_us
    ]
    avail = availability_points(data["avail"])
    slis = sli_summary(
        [p for p in points if p.ts_us >= report.start_us],
        [p for p in avail if p.ts_us >= report.start_us],
        durations,
        (success_target, latency_target_s, latency_quantile),
    )
    burns = {
        "interaction_success": evaluate_burn(points, now_us, success_target),
        "llm_availability": evaluate_burn(avail, now_us, success_target),
    }
    budgets = {k: slis[k]["budget_remaining_pct"] for k in burns}
    verdict = slo_verdict(burns, budgets, slis["interaction_latency"]["ok"])
    return _render_slo(window, verdict, slis, burns, reasons, data["notes"])


def sli_summary(
    inter: list[SliPoint],
    avail: list[SliPoint],
    durations_s: list[float],
    targets: tuple[float, float, float],
) -> dict[str, dict[str, Any]]:
    target, latency_target_s, q = targets

    def ratio(points: list[SliPoint]) -> dict[str, Any]:
        bad, total = sum(p.bad for p in points), sum(p.total for p in points)
        return {
            "good_ratio": round(1 - bad / total, 4) if total else None,
            "bad": bad,
            "total": total,
            "target": target,
            "budget_remaining_pct": budget_remaining_pct(bad, total, target),
        }

    value = quantile(durations_s, q)
    latency = {
        "quantile": q,
        "value_s": None if value is None else round(value, 1),
        "target_s": latency_target_s,
        "ok": None if value is None else value <= latency_target_s,
        "total": len(durations_s),
    }
    return {
        "interaction_success": ratio(inter),
        "interaction_latency": latency,
        "llm_availability": ratio(avail),
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:.2f}%"


def _render_slo(
    window: str,
    verdict: str,
    slis: dict[str, dict[str, Any]],
    burns: dict[str, list[dict[str, Any]]],
    reasons: dict[str, int],
    notes: list[str],
) -> ToolOutput:
    succ = slis["interaction_success"]
    lat = slis["interaction_latency"]
    avail = slis["llm_availability"]
    lat_value = None if lat["value_s"] is None else f"{fmt_num(lat['value_s'])} s"
    sli_rows = [
        [
            "interaction success",
            _pct(succ["good_ratio"]),
            _pct(succ["target"]),
            succ["budget_remaining_pct"],
            succ["total"],
        ],
        [
            f"interaction latency p{round(lat['quantile'] * 100)}",
            lat_value,
            f"≤ {fmt_num(lat['target_s'])} s",
            None,
            lat["total"],
        ],
        [
            "LLM availability",
            _pct(avail["good_ratio"]),
            _pct(avail["target"]),
            avail["budget_remaining_pct"],
            avail["total"],
        ],
    ]
    burn_rows = [
        [
            sli,
            f"{r['long']} + {r['short']}",
            r["long_burn"],
            r["short_burn"],
            f"{r['threshold']}×",
            r["action"],
            "**FIRING**" if r["fired"] else "no",
        ]
        for sli, rules in burns.items()
        for r in rules
    ]
    parts = [
        heading(f"Agent SLO report, last {window}"),
        f"**Verdict: {verdict}**",
        table(["SLI", "value", "target", "budget left %", "events"], sli_rows),
        "### Burn rates (multi-window, multi-burn-rate)\n"
        + table(
            ["SLI", "windows", "long burn", "short burn", "threshold", "action", "state"],
            burn_rows,
        ),
        f"Bad interactions by reason: {reasons['failed_tool']} with a failed tool, "
        f"{reasons['api_error']} with an API error, {reasons['slow']} over the latency target "
        "(an interaction counts once even with several reasons).",
        "_burn rate = bad ratio ÷ (1 − target); budget left = 1 − burn over the report window. "
        "Rules follow the SRE workbook (14.4×/1h+5m and 6×/6h+30m page; 3×/24h+2h and "
        "1×/72h+6h ticket), which assume a 30-day budget period._",
    ]
    parts += [f"_Note: {n}_" for n in notes]
    data = {
        "window": window,
        "verdict": verdict,
        "slis": slis,
        "burn_rates": burns,
        "bad_reasons": reasons,
        "notes": notes,
    }
    return ToolOutput("\n\n".join(parts), data)


# ---------------------------------------------------------------- 2. regression check


def robust_z(base: Sequence[float], recent: Sequence[float]) -> float | None:
    """Median shift in standard errors, with sigma estimated from the baseline MAD."""
    if not base or not recent:
        return None
    sigma = MAD_TO_SIGMA * (mad(base) or 0.0)
    if sigma == 0:
        mean = sum(base) / len(base)
        sigma = sum(abs(v - mean) for v in base) / len(base) * MEDIAN_SE
    if sigma == 0:
        return None
    se = MEDIAN_SE * sigma * math.sqrt(1 / len(base) + 1 / len(recent))
    return ((quantile(recent, 0.5) or 0) - (quantile(base, 0.5) or 0)) / se


def proportion_z(bad_b: float, n_b: int, bad_r: float, n_r: int) -> float | None:
    if n_b == 0 or n_r == 0:
        return None
    pooled = (bad_b + bad_r) / (n_b + n_r)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_b + 1 / n_r))
    return None if se == 0 else (bad_r / n_r - bad_b / n_b) / se


def center(values: Sequence[float], is_rate: bool) -> float | None:
    if not values:
        return None
    return 100 * sum(values) / len(values) if is_rate else quantile(values, 0.5)


def compare(base: Sequence[float], recent: Sequence[float], is_rate: bool) -> dict[str, Any]:
    """Baseline vs recent summary plus a verdict; higher is worse for every metric here."""
    b_c, r_c = center(base, is_rate), center(recent, is_rate)
    change = pct_change(r_c, b_c)
    if is_rate:
        z = proportion_z(sum(base), len(base), sum(recent), len(recent))
        practical = (r_c or 0) - (b_c or 0) >= MIN_RATE_CHANGE_PP
    else:
        z = robust_z(base, recent)
        practical = change is not None and change >= MIN_CHANGE_PCT
    if len(base) < MIN_SAMPLES or len(recent) < MIN_SAMPLES:
        verdict = "insufficient data"
    elif practical and (z is None or z >= Z_ALERT):
        verdict = "regression"
    elif z is not None and z <= -Z_ALERT:
        verdict = "improvement"
    else:
        verdict = "no significant change"
    return {
        "baseline": _summary(base, is_rate),
        "recent": _summary(recent, is_rate),
        "change_pct": change,
        "z": None if z is None else round(z, 2),
        "verdict": verdict,
    }


def _summary(values: Sequence[float], is_rate: bool) -> dict[str, Any]:
    if is_rate:
        return {
            "n": len(values),
            "rate_pct": _r(center(values, True)),
            "failures": int(sum(values)),
        }
    return {
        "n": len(values),
        "median": _r(quantile(values, 0.5)),
        "p95": _r(quantile(values, 0.95)),
        "mad": _r(mad(values)),
    }


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def breakdown(
    base: Sequence[Sample], recent: Sequence[Sample], dim: str, is_rate: bool
) -> list[dict[str, Any]]:
    """Per version (or model): baseline vs recent, share of samples and first-seen time."""
    overall = center([s.value for s in base], is_rate)
    rows = []
    for key in sorted({getattr(s, dim) for s in [*base, *recent]}):
        b = [s.value for s in base if getattr(s, dim) == key]
        r = [s.value for s in recent if getattr(s, dim) == key]
        b_share = len(b) / len(base) if base else 0.0
        rows.append(
            {
                dim: key,
                "baseline_n": len(b),
                "baseline": _r(center(b, is_rate)),
                "recent_n": len(r),
                "recent": _r(center(r, is_rate)),
                "baseline_share": round(b_share, 3),
                "recent_share": round(len(r) / len(recent), 3) if recent else 0.0,
                "vs_baseline_pct": pct_change(center(r, is_rate), overall),
                "self_change_pct": pct_change(center(r, is_rate), center(b, is_rate)),
                "new": len(b) == 0 or b_share < NEW_SHARE,
                "first_seen_us": min(s.ts_us for s in [*base, *recent] if getattr(s, dim) == key),
            }
        )
    return sorted(rows, key=lambda row: row["recent_n"], reverse=True)


def _cause_candidates(
    breakdowns: dict[str, list[dict[str, Any]]], overall_change: float | None
) -> list[tuple[str, str, dict[str, Any], float]]:
    cands = []
    for dim, rows in breakdowns.items():
        for row in rows:
            worse = row["vs_baseline_pct"]
            if row["recent_n"] < MIN_GROUP or worse is None or worse <= 0:
                continue
            if row["new"] and worse >= 0.5 * (overall_change or 0):
                cands.append(("deploy marker", dim, row, worse * row["recent_share"]))
            elif row["recent_share"] - row["baseline_share"] >= MIX_SHIFT:
                cands.append(("mix shift", dim, row, worse * row["recent_share"]))
    return cands


def likely_cause(
    breakdowns: dict[str, list[dict[str, Any]]], overall_change: float | None, tz: dt.tzinfo
) -> dict[str, Any]:
    """Point at a deploy marker (new version/model) or a mix shift that explains the change."""
    cands = _cause_candidates(breakdowns, overall_change)
    if not cands:
        return {
            "kind": "none",
            "text": "No deploy marker: the version/model mix did not change, so the shift is "
            "spread across existing versions and models (look upstream: provider latency, "
            "workload, repository size).",
        }
    kind, dim, row, _ = max(cands, key=lambda c: c[3])
    label = "Claude Code version" if dim == "version" else "model"
    steady = [
        r
        for r in breakdowns[dim]
        if r is not row
        and not r["new"]
        and r["recent_n"] >= MIN_GROUP
        and r["self_change_pct"] is not None
    ]
    old_regressed = [r for r in steady if r["self_change_pct"] >= MIN_CHANGE_PCT]
    text = (
        f"Likely cause — {kind}: {label} `{row[dim]}` (first seen "
        f"{fmt_local(row['first_seen_us'], tz)}, {round(100 * row['recent_share'])}% of recent "
        f"samples, {row['vs_baseline_pct']:+}% vs baseline)"
    )
    if old_regressed:
        listed = ", ".join(f"{r[dim]} {r['self_change_pct']:+}%" for r in old_regressed)
        text += (
            f", but unchanged {label}s also regressed ({listed}), "
            f"so part of it is not {dim}-specific."
        )
    elif steady:
        text += f" while unchanged {label}s held steady."
    else:
        text += "."
    return {"kind": kind, "dimension": dim, "group": row[dim], "text": text}


def build_samples(
    rows: Iterable[dict[str, Any]],
    models: dict[str, str],
    costs: dict[str, float] | None = None,
) -> list[Sample]:
    out = []
    for row in rows:
        ts = to_us(row.get("_timestamp"))
        tid = str(row.get("trace_id") or "")
        value = costs.get(tid) if costs is not None else to_float(row.get("v"))
        if ts is None or value is None:
            continue
        model = str(row.get("model") or models.get(tid) or "(unknown)")
        out.append(Sample(ts, value, str(row.get("version") or "(unknown)"), model))
    return out


def dominant_model(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    best: dict[str, tuple[float, str]] = {}
    for row in rows:
        tid, model = str(row.get("trace_id") or ""), str(row.get("model") or "")
        n = num(row.get("n"))
        if tid and model and n > best.get(tid, (-1.0, ""))[0]:
            best[tid] = (n, model)
    return {tid: model for tid, (_, model) in best.items()}


def _sample_sqls(deps: Deps, metric: str) -> list[str]:
    extra = ""
    if metric == "ttft":
        op, value, extra = tr.OP_LLM, "TRY_CAST(ttft_ms AS DOUBLE)", ", model"
    elif metric == "tool_failure_rate":
        op, value = tr.OP_EXEC, f"CASE WHEN {FAILED} THEN 1 ELSE 0 END"
    else:
        op, value = tr.OP_INTERACTION, "TRY_CAST(duration AS DOUBLE) / 1000000.0"
    return [
        f"SELECT _timestamp, trace_id, {value} AS v, {version} AS version{extra} "
        f"FROM {deps.claude} WHERE operation_name = {sql_str(op)} "
        f"ORDER BY _timestamp DESC LIMIT {RAW_CAP}"
        for version in (VERSION_COL, "'(unknown)'")
    ]


async def _trace_maps(
    deps: Deps, metric: str, rng: TimeRange
) -> tuple[dict[str, str], dict[str, float] | None, list[str]]:
    if metric == "ttft":
        return {}, None, []
    c, notes = deps.claude, []
    model_sql = (
        f"SELECT trace_id, model, COUNT(*) AS n FROM {c} WHERE operation_name = "
        f"{sql_str(tr.OP_LLM)} AND trace_id IS NOT NULL GROUP BY trace_id, model "
        f"LIMIT {RAW_CAP}"
    )
    model_rows, err = await deps.query_optional(model_sql, "traces", rng, RAW_CAP)
    if err:
        notes.append(f"model per trace unavailable: {err}")
    if metric != "cost_per_interaction":
        return dominant_model(model_rows), None, notes
    cost_sql = (
        f"SELECT trace_id, SUM(TRY_CAST(cost_usd AS DOUBLE)) AS cost FROM {c} "
        "WHERE event_name = 'api_request' AND trace_id IS NOT NULL "
        f"GROUP BY trace_id LIMIT {RAW_CAP}"
    )
    cost_rows, err = await deps.query_optional(cost_sql, "logs", rng, RAW_CAP)
    if err or not cost_rows:
        notes.append("api_request logs carry no trace_id, so cost cannot be tied to interactions")
    costs = {str(r["trace_id"]): num(r.get("cost")) for r in cost_rows if r.get("trace_id")}
    return dominant_model(model_rows), costs, notes


async def regression_check(
    deps: Deps, metric: str = "ttft", baseline: str = "7d", recent: str = "24h"
) -> ToolOutput:
    if metric not in REGRESSION_METRICS:
        raise ValueError(f"metric must be one of {list(REGRESSION_METRICS)}")
    recent_rng = _range_back(deps, parse_window(recent))
    base_rng = _range_back(deps, parse_window(baseline), recent_rng.start_us)
    sqls = _sample_sqls(deps, metric)
    (base_rows, n1), (recent_rows, n2), maps = await asyncio.gather(
        _first_ok(deps, sqls, "traces", base_rng, RAW_CAP),
        _first_ok(deps, sqls, "traces", recent_rng, RAW_CAP),
        _trace_maps(deps, metric, TimeRange(base_rng.start_us, recent_rng.end_us)),
    )
    models, costs, notes = maps
    notes += [n for n in (n1, n2) if n]
    notes += [
        f"{name} period capped at the latest {RAW_CAP} samples"
        for name, rows in (("baseline", base_rows), ("recent", recent_rows))
        if len(rows) >= RAW_CAP
    ]
    base = build_samples(base_rows, models, costs)
    rec = build_samples(recent_rows, models, costs)
    if not base and not rec:
        return empty(f"Regression check: {metric}", "No samples in either period.")
    is_rate = metric == "tool_failure_rate"
    result = compare([s.value for s in base], [s.value for s in rec], is_rate)
    dims = {d: breakdown(base, rec, d, is_rate) for d in ("version", "model")}
    cause = (
        likely_cause(dims, result["change_pct"], deps.settings.tz)
        if result["verdict"] == "regression"
        else {"kind": "none", "text": "No regression to attribute."}
    )
    meta = {"metric": metric, "baseline_window": baseline, "recent_window": recent}
    return _render_regression(deps, meta, result, dims, cause, notes)


def _render_regression(
    deps: Deps,
    meta: dict[str, str],
    result: dict[str, Any],
    dims: dict[str, list[dict[str, Any]]],
    cause: dict[str, Any],
    notes: list[str],
) -> ToolOutput:
    metric = meta["metric"]
    unit, is_rate = METRIC_UNITS[metric], metric == "tool_failure_rate"
    stat = "rate %" if is_rate else f"median {unit}"
    b, r = result["baseline"], result["recent"]
    if is_rate:
        cols = ["period", "n", "failure rate %", "failures"]
        overview = [
            ["baseline", b["n"], b["rate_pct"], b["failures"]],
            ["recent", r["n"], r["rate_pct"], r["failures"]],
        ]
    else:
        cols = ["period", "n", f"median {unit}", f"p95 {unit}"]
        overview = [
            ["baseline", b["n"], b["median"], b["p95"]],
            ["recent", r["n"], r["median"], r["p95"]],
        ]
    parts = [
        heading(
            f"Regression check: {metric}",
            f"recent {meta['recent_window']} vs the {meta['baseline_window']} before it",
        ),
        f"**{result['verdict'].upper()}** — change {result['change_pct']}%, z = {result['z']} "
        f"(regression when z ≥ {Z_ALERT} and the change is practically large)",
        table(cols, overview),
        f"**{cause['text']}**",
    ]
    for dim, rows in dims.items():
        body = [
            [
                x[dim],
                x["baseline_n"],
                x["baseline"],
                x["recent_n"],
                x["recent"],
                x["recent_share"],
                x["vs_baseline_pct"],
                "yes" if x["new"] else "",
                fmt_local(x["first_seen_us"], deps.settings.tz),
            ]
            for x in rows[:12]
        ]
        cols_dim = [
            dim,
            "base n",
            f"base {stat}",
            "recent n",
            f"recent {stat}",
            "recent share",
            "Δ vs baseline %",
            "new",
            "first seen",
        ]
        parts.append(f"### By {dim}\n" + table(cols_dim, body))
    parts.append(
        "_z: robust (median shift ÷ 1.2533·1.4826·MAD·√(1/n₁+1/n₂)) for continuous metrics, "
        "two-proportion z for the failure rate._"
    )
    parts += [f"_Note: {n}_" for n in notes]
    data = {
        **meta,
        "unit": unit,
        **result,
        "likely_cause": cause,
        "by_version": dims["version"],
        "by_model": dims["model"],
        "notes": notes,
    }
    return ToolOutput("\n\n".join(parts), data)


# ---------------------------------------------------------------- 3. incident timeline


def parse_when(text: str, tz: dt.tzinfo) -> int:
    """ISO 8601 (naive means the configured local zone) or epoch digits, to µs."""
    raw = (text or "").strip()
    if raw.isdigit():
        return to_us(int(raw)) or 0
    try:
        when = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"cannot parse time {text!r}; use ISO 8601 like 2026-10-08T14:30 or epoch seconds"
        ) from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return int(when.timestamp() * US_PER_S)


def incident_range(
    start: str | None, end: str | None, window: str, now_s: float, tz: dt.tzinfo
) -> TimeRange:
    span_us = parse_window(window) * US_PER_S
    now_us = int(now_s * US_PER_S)
    if start:
        s = parse_when(start, tz)
        e = parse_when(end, tz) if end else min(s + span_us, now_us)
    elif end:
        e = parse_when(end, tz)
        s = e - span_us
    else:
        s, e = now_us - span_us, now_us
    if e <= s:
        raise ValueError("end must be after start (and start must be in the past)")
    if e - s > INCIDENT_MAX_S * US_PER_S:
        raise WindowError("an incident window is capped at 7d")
    return TimeRange(s, e)


def detect_spikes(
    buckets: Iterable[dict[str, Any]], baseline_p95: float | None
) -> list[TimelineEvent]:
    """5-minute TTFT p95 buckets above SPIKE_FACTOR × the previous day's p95."""
    if not baseline_p95:
        return []
    out = []
    for row in buckets:
        ts, p95, n = to_us(row.get("t")), to_float(row.get("p95")), int(num(row.get("n")))
        if ts is None or p95 is None or n < SPIKE_MIN_CALLS:
            continue
        if p95 > SPIKE_FACTOR * baseline_p95:
            detail = (
                f"TTFT p95 {fmt_num(p95, 0)} ms over 5 min "
                f"(baseline p95 {fmt_num(baseline_p95, 0)} ms, n={n})"
            )
            out.append(TimelineEvent(ts, "latency_spike", detail))
    return out


def api_events(rows: Iterable[dict[str, Any]]) -> list[TimelineEvent]:
    out = []
    for row in rows:
        ts = to_us(row.get("_timestamp"))
        if ts is None:
            continue
        model = row.get("model") or "(unknown)"
        if row.get("event_name") == RETRIES_EXHAUSTED:
            kind, detail = "retries_exhausted", f"retries exhausted on {model}"
        else:
            kind, detail = "api_error", f"API error {row.get('status') or '(none)'} on {model}"
        session, trace = str(row.get("session_id") or ""), str(row.get("trace_id") or "")
        out.append(TimelineEvent(ts, kind, detail, session, trace))
    return out


def tool_events(rows: Iterable[dict[str, Any]], parents: dict[str, str]) -> list[TimelineEvent]:
    out = []
    for row in rows:
        ts = to_us(row.get("_timestamp"))
        if ts is None:
            continue
        tool = (
            row.get("tool_name")
            or parents.get(str(row.get("reference_parent_span_id") or ""))
            or "(unknown)"
        )
        detail = f"{tool} failed ({row.get('error_class') or 'no error_class'})"
        session, trace = str(row.get("session_id") or ""), str(row.get("trace_id") or "")
        out.append(TimelineEvent(ts, "tool_failure", detail, session, trace))
    return out


def mcp_events(rows: Iterable[dict[str, Any]]) -> list[TimelineEvent]:
    out = []
    for row in rows:
        ts = to_us(row.get("_timestamp"))
        if ts is not None:
            detail = f"MCP server {row.get('server') or '(redacted)'} connection failed"
            out.append(TimelineEvent(ts, "mcp_failure", detail, str(row.get("session_id") or "")))
    return out


def collapse(
    events: Sequence[TimelineEvent], gap_us: int = COLLAPSE_GAP_US
) -> list[dict[str, Any]]:
    """Merge runs of the same kind+detail whose events are at most `gap_us` apart."""
    out: list[dict[str, Any]] = []
    open_runs: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: (e.ts_us, e.kind)):
        key = (ev.kind, ev.detail)
        run = open_runs.get(key)
        if run is not None and ev.ts_us - run["last_us"] <= gap_us:
            run["last_us"], run["count"] = ev.ts_us, run["count"] + 1
            continue
        run = {
            "first_us": ev.ts_us,
            "last_us": ev.ts_us,
            "kind": ev.kind,
            "detail": ev.detail,
            "count": 1,
            "session_id": ev.session_id,
            "trace_id": ev.trace_id,
        }
        open_runs[key] = run
        out.append(run)
    return out


def contributing_factors(events: Sequence[TimelineEvent], active_sessions: int) -> list[str]:
    if not events:
        return []
    kinds = Counter(e.kind for e in events)
    top_kind, top_n = kinds.most_common(1)[0]
    facts = [f"{top_n} of {len(events)} bad events were `{top_kind}`."]
    details = Counter(e.detail for e in events if e.kind != "latency_spike")
    facts += [f"`{detail}` occurred {n}×." for detail, n in details.most_common(3)]
    spikes = [e.ts_us for e in events if e.kind == "latency_spike"]
    errors = [e.ts_us for e in events if e.kind in ("api_error", "retries_exhausted")]
    if any(abs(s - x) <= COLLAPSE_GAP_US for s in spikes for x in errors):
        facts.append("TTFT spikes overlapped API errors within 5 minutes.")
    sessions = {e.session_id for e in events if e.session_id}
    if active_sessions:
        facts.append(f"{len(sessions)} of {active_sessions} active sessions saw a bad event.")
    return facts


def follow_up_questions(kinds: set[str]) -> list[str]:
    questions = {
        "api_error": "Did the provider's status page report an incident in this window, and "
        "should we alert on API error rate before users notice?",
        "retries_exhausted": "Are retry budgets/backoff right for these errors, and should a "
        "fallback model take over when retries are exhausted?",
        "tool_failure": "Were the failing tools affected by an environment change (credentials, "
        "network, dependency, repository state) at the first-bad time?",
        "mcp_failure": "Was the MCP server restarted, upgraded or rate-limited, and does the "
        "agent degrade gracefully without it?",
        "latency_spike": "Did a model, region or prompt-size change coincide with the TTFT spike?",
    }
    out = [questions[k] for k in sorted(kinds) if k in questions]
    out.append("What would have detected this sooner (see recommend_alerts)?")
    return out


async def _spike_events(deps: Deps, rng: TimeRange) -> tuple[list[TimelineEvent], str | None]:
    ttft = "approx_percentile_cont(TRY_CAST(ttft_ms AS DOUBLE), 0.95)"
    llm = f"operation_name = {sql_str(tr.OP_LLM)}"
    base_sql = f"SELECT {ttft} AS p95 FROM {deps.claude} WHERE {llm}"
    bucket_sql = (
        f"SELECT histogram(_timestamp, '5 minute') AS t, {ttft} AS p95, COUNT(*) AS n "
        f"FROM {deps.claude} WHERE {llm} GROUP BY t ORDER BY t LIMIT 3000"
    )
    prior = TimeRange(rng.start_us - DAY_S * US_PER_S, rng.start_us)
    (base, n1), (buckets, n2) = await asyncio.gather(
        deps.query_optional(base_sql, "traces", prior, 1),
        deps.query_optional(bucket_sql, "traces", rng, 3000),
    )
    baseline = to_float(base[0].get("p95")) if base else None
    return detect_spikes(buckets, baseline), n1 or n2


async def _incident_events(
    deps: Deps, rng: TimeRange
) -> tuple[list[TimelineEvent], int, list[str]]:
    c = deps.claude
    api_variants = [
        f"SELECT _timestamp, event_name, session_id, trace_id, COALESCE(model, '(unknown)') AS "
        f"model, {status} AS status FROM {c} WHERE event_name IN ({in_list(API_FAIL_EVENTS)}) "
        f"ORDER BY _timestamp LIMIT {INCIDENT_CAP}"
        for status in ("CAST(status_code AS VARCHAR)", "'(none)'")
    ]
    mcp_sql = (
        f"SELECT _timestamp, session_id, COALESCE(server_name, '(redacted)') AS server FROM {c} "
        "WHERE event_name = 'mcp_server_connection' AND status = 'failed' "
        f"ORDER BY _timestamp LIMIT {INCIDENT_CAP}"
    )
    tool_sql = (
        "SELECT _timestamp, trace_id, session_id, tool_name, error_class, "
        f"reference_parent_span_id FROM {c} WHERE operation_name = {sql_str(tr.OP_EXEC)} "
        f"AND {FAILED} ORDER BY _timestamp LIMIT {INCIDENT_CAP}"
    )
    active_sql = (
        f"SELECT COUNT(DISTINCT session_id) AS sessions FROM {c} WHERE event_name = 'api_request'"
    )
    (api, n1), (mcp, n2), (tools, n3), (active, n4), (spikes, n5) = await asyncio.gather(
        _first_ok(deps, api_variants, "logs", rng, INCIDENT_CAP),
        deps.query_optional(mcp_sql, "logs", rng, INCIDENT_CAP),
        deps.query_optional(tool_sql, "traces", rng, INCIDENT_CAP),
        deps.query_optional(active_sql, "logs", rng, 1),
        _spike_events(deps, rng),
    )
    parents = await resolve_parent_tools(deps, rng, tools) if tools else {}
    events = [*api_events(api), *mcp_events(mcp), *tool_events(tools, parents), *spikes]
    notes = [n for n in (n1, n2, n3, n4, n5) if n]
    sessions = int(_first_row(active, "sessions"))
    return sorted(events, key=lambda e: (e.ts_us, e.kind)), sessions, notes


async def incident_timeline(
    deps: Deps, start: str | None = None, end: str | None = None, window: str = "2h"
) -> ToolOutput:
    tz = deps.settings.tz
    rng = incident_range(start, end, window, deps.clock(), tz)
    events, active, notes = await _incident_events(deps, rng)
    span = f"{fmt_local(rng.start_us, tz)} → {fmt_local(rng.end_us, tz)} ({deps.settings.tz_name})"
    if not events:
        md = f"{heading('Incident timeline', span)}\n\nNo bad events in this window."
        data = {"start_us": rng.start_us, "end_us": rng.end_us, "events": 0, "timeline": []}
        return ToolOutput(md, {**data, "notes": notes})
    return _render_incident(deps, rng, span, events, active, notes)


def _incident_summary(events: list[TimelineEvent], active: int, tz: dt.tzinfo) -> tuple[str, str]:
    first, last = events[0], events[-1]
    mix = ", ".join(f"{k} ×{n}" for k, n in Counter(e.kind for e in events).most_common())
    minutes = round((last.ts_us - first.ts_us) / MIN_US, 1)
    summary = (
        f"Between {fmt_local(first.ts_us, tz, True)} and {fmt_local(last.ts_us, tz, True)} "
        f"({minutes} min) the agent recorded {len(events)} bad events ({mix})."
    )
    sessions = len({e.session_id for e in events if e.session_id})
    traces = len({e.trace_id for e in events if e.trace_id})
    of_active = f" of {active} active" if active else ""
    impact = f"{sessions} session(s){of_active} and {traces} interaction trace(s) affected."
    return summary, impact


def _render_incident(
    deps: Deps,
    rng: TimeRange,
    span: str,
    events: list[TimelineEvent],
    active: int,
    notes: list[str],
) -> ToolOutput:
    tz = deps.settings.tz
    first, last = events[0], events[-1]
    runs = collapse(events)
    facts = contributing_factors(events, active)
    questions = follow_up_questions({e.kind for e in events})
    summary, impact = _incident_summary(events, active, tz)
    rows = [
        [
            fmt_local(r["first_us"], tz, True),
            r["kind"],
            r["detail"],
            r["count"],
            r["session_id"] or None,
            r["trace_id"] or None,
        ]
        for r in runs[:TIMELINE_ROWS]
    ]
    parts = [
        heading("Incident timeline — postmortem draft (blameless)", span),
        f"**Summary.** {summary}",
        f"**Impact.** {impact}",
        f"**First bad:** {fmt_local(first.ts_us, tz, True)} — {first.detail}  \n"
        f"**Last bad:** {fmt_local(last.ts_us, tz, True)} — {last.detail}",
        "### Timeline\n" + table(["time", "kind", "event", "count", "session", "trace"], rows),
        "### Contributing factors (observed facts only)\n" + "\n".join(f"- {f}" for f in facts),
        "### Follow-ups (questions, not blame)\n" + "\n".join(f"- {q}" for q in questions),
        "### Detection / Response / Lessons\n- _to be filled in by the responders_",
    ]
    if len(runs) > TIMELINE_ROWS:
        parts.insert(5, f"_Timeline shows the first {TIMELINE_ROWS} of {len(runs)} entries._")
    parts += [f"_Note: {n}_" for n in notes]
    data = {
        "start_us": rng.start_us,
        "end_us": rng.end_us,
        "events": len(events),
        "first_bad": _event_dict(first, tz),
        "last_bad": _event_dict(last, tz),
        "by_kind": dict(Counter(e.kind for e in events)),
        "affected_sessions": sorted({e.session_id for e in events if e.session_id})[:50],
        "affected_traces": sorted({e.trace_id for e in events if e.trace_id})[:50],
        "active_sessions": active,
        "timeline": [{**r, "time": fmt_local(r["first_us"], tz, True)} for r in runs],
        "contributing_factors": facts,
        "follow_ups": questions,
        "notes": notes,
    }
    return ToolOutput("\n\n".join(parts), data)


def _event_dict(ev: TimelineEvent, tz: dt.tzinfo) -> dict[str, Any]:
    return {
        "ts_us": ev.ts_us,
        "time": fmt_local(ev.ts_us, tz, True),
        "kind": ev.kind,
        "detail": ev.detail,
        "session_id": ev.session_id,
        "trace_id": ev.trace_id,
    }


# ---------------------------------------------------------------- 4. budget forecast


def month_bounds(now_s: float, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    now = dt.datetime.fromtimestamp(now_s, tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    return now, start, end


def crossing_date(points: Sequence[tuple[int, float]], budget: float, tz: dt.tzinfo) -> str | None:
    total = 0.0
    for ts, cost in sorted(points):
        total += cost
        if total >= budget:
            return dt.datetime.fromtimestamp(ts / US_PER_S, tz).date().isoformat()
    return None


def forecast_budget(
    budget: float,
    mtd: float,
    daily_rate: float,
    now: dt.datetime,
    month_end: dt.datetime,
    crossed_on: str | None = None,
) -> dict[str, Any]:
    """Linear month-end projection from the trailing daily run-rate."""
    days_left = max(0.0, (month_end - now).total_seconds() / DAY_S)
    projected = mtd + daily_rate * days_left
    remaining = budget - mtd
    if remaining <= 0:
        status, exhaust = "exhausted", crossed_on or now.date().isoformat()
    elif daily_rate <= 0:
        status, exhaust = "no recent spend", None
    else:
        at = now + dt.timedelta(days=remaining / daily_rate)
        exhaust = at.date().isoformat()
        status = "exhausts before month end" if at < month_end else "within budget"
    cap = remaining / days_left if days_left > 0 and remaining > 0 else 0.0
    return {
        "budget_usd": round(budget, 2),
        "spent_mtd_usd": round(mtd, 2),
        "daily_run_rate_usd": round(daily_rate, 2),
        "days_left": round(days_left, 2),
        "projected_month_end_usd": round(projected, 2),
        "projected_pct_of_budget": round(100 * projected / budget, 1),
        "exhaustion_date": exhaust,
        "status": status,
        "daily_cap_to_land_on_budget_usd": round(cap, 2),
    }


def cap_recommendation(fc: dict[str, Any]) -> str:
    rate, cap = fc["daily_run_rate_usd"], fc["daily_cap_to_land_on_budget_usd"]
    if fc["status"] == "exhausted":
        return (
            f"Budget exhausted on {fc['exhaustion_date']}. Pause non-critical agent work or "
            "raise the budget explicitly, and set a hard daily cap before next month starts."
        )
    if fc["projected_month_end_usd"] <= fc["budget_usd"]:
        return (
            f"On track ({fc['projected_pct_of_budget']}% of budget projected). Keep a soft alert "
            f"at 80% (${0.8 * fc['budget_usd']:,.2f}) and a hard daily cap of ${cap:,.2f}."
        )
    return (
        f"Over budget: cap spend at ${cap:,.2f}/day for the remaining {fc['days_left']} days "
        f"(run-rate ${rate:,.2f}/day, {pct_change(cap, rate)}%), or the budget runs out on "
        f"{fc['exhaustion_date']}. Consider per-session caps and cheaper models for routine work."
    )


async def _budget_inputs(deps: Deps, month: TimeRange, trailing: TimeRange) -> dict[str, Any]:
    c, cost = deps.claude, "SUM(TRY_CAST(cost_usd AS DOUBLE))"
    api = "event_name = 'api_request'"
    top = f"ORDER BY cost DESC LIMIT {TOP_SPENDERS}"
    sqls: dict[str, tuple[str, str, TimeRange, int]] = {
        "mtd": (
            f"SELECT histogram(_timestamp, '1 hour') AS t, {cost} AS cost FROM {c} WHERE {api} "
            "GROUP BY t ORDER BY t LIMIT 1000",
            "logs",
            month,
            1000,
        ),
        "trailing": (
            f"SELECT {cost} AS cost, COUNT(*) AS calls FROM {c} WHERE {api}",
            "logs",
            trailing,
            1,
        ),
        "interactions": (
            f"SELECT COUNT(*) AS n FROM {c} WHERE operation_name = {sql_str(tr.OP_INTERACTION)}",
            "traces",
            trailing,
            1,
        ),
        "failed": (
            f"SELECT COUNT(DISTINCT trace_id) AS n FROM {c} WHERE operation_name = "
            f"{sql_str(tr.OP_EXEC)} AND {FAILED}",
            "traces",
            trailing,
            1,
        ),
        "traces": (
            f"SELECT trace_id, {cost} AS cost FROM {c} WHERE {api} AND trace_id IS NOT NULL "
            f"GROUP BY trace_id {top}",
            "logs",
            trailing,
            TOP_SPENDERS,
        ),
        "sessions": (
            f"SELECT session_id, {cost} AS cost FROM {c} WHERE {api} AND session_id IS NOT NULL "
            f"GROUP BY session_id {top}",
            "logs",
            trailing,
            TOP_SPENDERS,
        ),
    }
    results = await asyncio.gather(
        *(
            deps.query_optional(sql, cast(StreamType, st), rng, size)
            for sql, st, rng, size in sqls.values()
        )
    )
    out: dict[str, Any] = {k: rows for k, (rows, _) in zip(sqls, results, strict=True)}
    out["notes"] = [f"{k}: {err}" for k, (_, err) in zip(sqls, results, strict=True) if err]
    return out


async def budget_forecast(deps: Deps, monthly_budget_usd: float, window: str = "7d") -> ToolOutput:
    if not monthly_budget_usd or monthly_budget_usd <= 0:
        raise ValueError("monthly_budget_usd must be greater than 0")
    tz = deps.settings.tz
    trailing = window_range(window, deps.clock())
    now, m_start, m_end = month_bounds(deps.clock(), tz)
    month = TimeRange(int(m_start.timestamp() * US_PER_S), trailing.end_us)
    data = await _budget_inputs(deps, month, trailing)
    points = series(data["mtd"], "cost")
    trailing_cost = _first_row(data["trailing"], "cost")
    rate = trailing_cost / (trailing.seconds / DAY_S)
    fc = forecast_budget(
        monthly_budget_usd,
        sum(v for _, v in points),
        rate,
        now,
        m_end,
        crossing_date(points, monthly_budget_usd, tz),
    )
    interactions = int(_first_row(data["interactions"], "n"))
    successes = max(0, interactions - int(_first_row(data["failed"], "n")))
    fc["successful_interactions"] = successes
    fc["cost_per_successful_interaction_usd"] = (
        round(trailing_cost / successes, 4) if successes else None
    )
    fc["recommendation"] = cap_recommendation(fc)
    return _render_budget(deps, window, fc, data)


def _render_budget(deps: Deps, window: str, fc: dict[str, Any], data: dict[str, Any]) -> ToolOutput:
    tops = {
        key: [
            {
                f"{key[:-1]}_id": str(r.get(f"{key[:-1]}_id")),
                "cost_usd": round(num(r.get("cost")), 4),
            }
            for r in data[key]
        ]
        for key in ("traces", "sessions")
    }
    per_success = fc["cost_per_successful_interaction_usd"]
    projected = (
        f"${fc['projected_month_end_usd']:,.2f} "
        f"({fc['projected_pct_of_budget']}% of ${fc['budget_usd']:,.2f})"
    )
    summary = [
        ["spent this month", f"${fc['spent_mtd_usd']:,.2f}"],
        [f"daily run-rate ({window} trailing)", f"${fc['daily_run_rate_usd']:,.2f}"],
        ["projected month end", projected],
        ["budget exhausted on", fc["exhaustion_date"] or "–"],
        [
            "cost per successful interaction",
            None if per_success is None else f"${per_success:,.4f}",
        ],
    ]
    parts = [
        heading("Budget forecast", f"calendar month in {deps.settings.tz_name}; linear projection"),
        f"**{fc['status'].upper()}** — {fc['recommendation']}",
        table(["metric", "value"], summary),
    ]
    for key in ("traces", "sessions"):
        if tops[key]:
            rows = [list(t.values()) for t in tops[key]]
            parts.append(
                f"### Top {TOP_SPENDERS} costliest {key} ({window})\n"
                + table([f"{key[:-1]}_id", "cost $"], rows)
            )
    parts.append("_successful interaction = an interaction with no failed tool execution._")
    parts += [f"_Note: {n}_" for n in data["notes"]]
    out = {
        "window": window,
        **fc,
        "top_traces": tops["traces"],
        "top_sessions": tops["sessions"],
        "notes": data["notes"],
    }
    return ToolOutput("\n\n".join(parts), out)


# ---------------------------------------------------------------- 5. alert recommendations


def alert_json(
    name: str,
    description: str,
    stream: tuple[str, str],
    sql: str,
    schedule: tuple[int, int, int],
) -> dict[str, Any]:
    """An OpenObserve scheduled-alert body shaped like claude-code/alerts/*.json.

    `stream` is (name, type); `schedule` is (period, frequency, silence) in minutes.
    """
    period, frequency, silence = schedule
    return {
        "name": name,
        "description": description,
        "stream_type": stream[1],
        "stream_name": stream[0],
        "is_real_time": False,
        "query_condition": {"type": "sql", "sql": sql, "conditions": None, "vrl_function": None},
        "trigger_condition": {
            "period": period,
            "operator": ">=",
            "threshold": 1,
            "frequency": frequency,
            "frequency_type": "minutes",
            "silence": silence,
            "cron": "",
            "align_time": False,
        },
        "destinations": [ALERT_DESTINATION],
        "enabled": False,
        "tz_offset": 0,
        "row_template": "",
        "tags": list(ALERT_TAGS),
        "creates_incident": False,
    }


def backtest(values: Iterable[float], threshold: float) -> int:
    return sum(1 for v in values if v > threshold)


def _draft(
    alert: dict[str, Any], why: tuple[str, str, str], fires: int | None, unit: str, window: str
) -> dict[str, Any]:
    signal, threshold, reason = why
    noise = (
        "no baseline data; expect to tune it after the first week"
        if fires is None
        else f"would have fired {fires}× in the last {window} ({unit} backtest)"
    )
    rationale = {"signal": signal, "threshold": threshold, "why": reason, "expected_noise": noise}
    return {"alert": alert, "rationale": rationale}


def cost_alert(stream: str, daily: Sequence[float], window: str) -> dict[str, Any]:
    active = [d for d in daily if d > 0]
    limit = round(max((quantile(active, 0.95) or 0) * 1.5, 1.0), 2)
    expr = "SUM(TRY_CAST(cost_usd AS DOUBLE))"
    sql = (
        f"SELECT ROUND({expr}, 2) AS usd_24h FROM {quote_ident(stream)} "
        f"WHERE event_name = 'api_request' HAVING {expr} > {limit}"
    )
    alert = alert_json(
        "claude_code_slo_daily_cost",
        f"24 h spend above ${limit} (1.5 × p95 of active days).",
        (stream, "logs"),
        sql,
        (1440, 60, 720),
    )
    why = (
        "spend per 24 h",
        f"> ${limit}",
        "1.5 × p95 of daily spend on active days catches runaway loops without paging on a "
        "busy day.",
    )
    return _draft(alert, why, backtest(active, limit) if active else None, "daily", window)


def api_error_alert(
    stream: str, hourly: Sequence[tuple[float, float]], window: str
) -> dict[str, Any]:
    """hourly = (failed API events, all API events) per hour."""
    rates = [100 * e / t for e, t in hourly if t >= MIN_ALERT_VOLUME]
    limit = round(min(max(2 * (quantile(rates, 0.95) or 0), 5.0), 50.0), 1)
    bad = f"SUM(CASE WHEN event_name IN ({in_list(API_FAIL_EVENTS)}) THEN 1 ELSE 0 END)"
    sql = (
        f"SELECT ROUND(100.0 * {bad} / NULLIF(COUNT(*), 0), 1) AS error_pct, COUNT(*) AS events "
        f"FROM {quote_ident(stream)} WHERE event_name IN ('api_request', "
        f"{in_list(API_FAIL_EVENTS)}) HAVING COUNT(*) >= {MIN_ALERT_VOLUME} "
        f"AND 100.0 * {bad} / COUNT(*) > {limit}"
    )
    alert = alert_json(
        "claude_code_slo_api_error_rate",
        f"API error rate above {limit}% over 1 h (2 × hourly p95).",
        (stream, "logs"),
        sql,
        (60, 15, 60),
    )
    why = (
        "API error %, 1 h",
        f"> {limit}% with ≥ {MIN_ALERT_VOLUME} calls",
        "2 × the hourly p95 error rate; the volume floor stops one failed call in a quiet hour "
        "from paging.",
    )
    return _draft(alert, why, backtest(rates, limit) if rates else None, "hourly", window)


def ttft_alert(
    stream: str, p95: float | None, hourly_p95: Sequence[float], window: str
) -> dict[str, Any]:
    limit = round(max(2 * (p95 or 0), 2000.0))
    expr = "approx_percentile_cont(TRY_CAST(ttft_ms AS DOUBLE), 0.95)"
    sql = (
        f"SELECT ROUND({expr}) AS ttft_p95_ms, COUNT(*) AS calls FROM {quote_ident(stream)} "
        f"WHERE operation_name = {sql_str(tr.OP_LLM)} HAVING COUNT(*) >= 5 AND {expr} > {limit}"
    )
    alert = alert_json(
        "claude_code_slo_ttft_p95",
        f"TTFT p95 above {limit} ms over 30 min (2 × baseline p95).",
        (stream, "traces"),
        sql,
        (30, 15, 60),
    )
    why = (
        "TTFT p95, 30 min",
        f"> {limit} ms",
        "2 × the baseline TTFT p95 (floor 2 s): a provider slowdown users feel, not jitter.",
    )
    fires = backtest(hourly_p95, limit) if p95 else None
    return _draft(alert, why, fires, "hourly p95", window)


def tool_failure_alert(
    stream: str, hourly: Sequence[tuple[float, float]], window: str
) -> dict[str, Any]:
    """hourly = (failed executions, all executions) per hour."""
    total = sum(t for _, t in hourly)
    overall = 100 * sum(f for f, _ in hourly) / total if total else 0.0
    rates = [100 * f / t for f, t in hourly if t >= MIN_ALERT_VOLUME]
    limit = round(min(max(2 * overall, quantile(rates, 0.95) or 0, 10.0), 80.0), 1)
    bad = f"SUM(CASE WHEN {FAILED} THEN 1 ELSE 0 END)"
    sql = (
        f"SELECT ROUND(100.0 * {bad} / NULLIF(COUNT(*), 0), 1) AS fail_pct, COUNT(*) AS execs "
        f"FROM {quote_ident(stream)} WHERE operation_name = {sql_str(tr.OP_EXEC)} "
        f"HAVING COUNT(*) >= {MIN_ALERT_VOLUME} AND 100.0 * {bad} / COUNT(*) > {limit}"
    )
    alert = alert_json(
        "claude_code_slo_tool_failure_rate",
        f"Tool failure rate above {limit}% over 1 h.",
        (stream, "traces"),
        sql,
        (60, 15, 120),
    )
    why = (
        "tool failure %, 1 h",
        f"> {limit}% with ≥ {MIN_ALERT_VOLUME} executions",
        "max(2 × overall rate, hourly p95, 10%): failures are normal in agent loops, a step "
        "change is not.",
    )
    return _draft(alert, why, backtest(rates, limit) if rates else None, "hourly", window)


def permission_wait_alert(stream: str, daily_min: Sequence[float], window: str) -> dict[str, Any]:
    active = [d for d in daily_min if d > 0]
    limit = round(max((quantile(active, 0.95) or 0) * 1.5, 5.0), 1)
    expr = "SUM(TRY_CAST(duration AS DOUBLE)) / 60000000.0"
    sql = (
        f"SELECT ROUND({expr}, 1) AS wait_min_24h FROM {quote_ident(stream)} "
        f"WHERE operation_name = {sql_str(tr.OP_BLOCKED)} HAVING {expr} > {limit}"
    )
    alert = alert_json(
        "claude_code_slo_permission_wait",
        f"More than {limit} min/day waiting on permission prompts.",
        (stream, "traces"),
        sql,
        (1440, 60, 720),
    )
    why = (
        "permission wait minutes / 24 h",
        f"> {limit} min",
        "toil signal: 1.5 × p95 of daily wait means allow-rules are missing "
        "(see permission_wait_report).",
    )
    return _draft(alert, why, backtest(active, limit) if active else None, "daily", window)


def mcp_alert(stream: str, hourly: Sequence[float], window: str, available: bool) -> dict[str, Any]:
    limit = max(3, math.ceil(quantile(hourly, 0.99) or 0) + 1)
    sql = (
        f"SELECT COALESCE(server_name, '(redacted)') AS server, COUNT(*) AS failures "
        f"FROM {quote_ident(stream)} WHERE event_name = 'mcp_server_connection' "
        f"AND status = 'failed' GROUP BY 1 HAVING COUNT(*) >= {limit}"
    )
    alert = alert_json(
        "claude_code_slo_mcp_failures",
        f"An MCP server failed to connect {limit}+ times in 30 minutes.",
        (stream, "logs"),
        sql,
        (30, 30, 120),
    )
    why = (
        "MCP connection failures / 30 min",
        f"≥ {limit}",
        "above the hourly p99 of failures: a dead MCP server silently removes tools from the "
        "agent.",
    )
    fires = backtest(hourly, limit - 1) if available else None
    return _draft(alert, why, fires, "hourly", window)


async def _alert_inputs(deps: Deps, rng: TimeRange) -> dict[str, Any]:
    c, hist = deps.claude, "histogram(_timestamp, '1 hour') AS t"
    fails = in_list(API_FAIL_EVENTS)
    ttft = "approx_percentile_cont(TRY_CAST(ttft_ms AS DOUBLE), 0.95)"
    llm = f"operation_name = {sql_str(tr.OP_LLM)}"
    tail = "GROUP BY t ORDER BY t LIMIT 10000"
    sqls = {
        "logs": (
            f"SELECT {hist}, SUM(CASE WHEN event_name = 'api_request' THEN "
            "TRY_CAST(cost_usd AS DOUBLE) ELSE 0 END) AS cost, COUNT(*) AS events, "
            f"SUM(CASE WHEN event_name IN ({fails}) THEN 1 ELSE 0 END) AS errors "
            f"FROM {c} WHERE event_name IN ('api_request', {fails}) {tail}",
            "logs",
        ),
        "exec": (
            f"SELECT {hist}, COUNT(*) AS execs, SUM(CASE WHEN {FAILED} THEN 1 ELSE 0 END) "
            f"AS failed FROM {c} WHERE operation_name = {sql_str(tr.OP_EXEC)} {tail}",
            "traces",
        ),
        "blocked": (
            f"SELECT {hist}, SUM(TRY_CAST(duration AS DOUBLE)) AS wait_us FROM {c} "
            f"WHERE operation_name = {sql_str(tr.OP_BLOCKED)} {tail}",
            "traces",
        ),
        "ttft_hourly": (f"SELECT {hist}, {ttft} AS p95 FROM {c} WHERE {llm} {tail}", "traces"),
        "ttft": (f"SELECT {ttft} AS p95 FROM {c} WHERE {llm}", "traces"),
        "mcp": (
            f"SELECT {hist}, COUNT(*) AS failures FROM {c} WHERE event_name = "
            f"'mcp_server_connection' AND status = 'failed' {tail}",
            "logs",
        ),
    }
    results = await asyncio.gather(
        *(deps.query_optional(sql, cast(StreamType, st), rng, 10000) for sql, st in sqls.values())
    )
    out: dict[str, Any] = {k: rows for k, (rows, _) in zip(sqls, results, strict=True)}
    out["errors"] = {k: err for k, (_, err) in zip(sqls, results, strict=True) if err}
    return out


async def recommend_alerts(deps: Deps, window: str = "14d") -> ToolOutput:
    rng = window_range(window, deps.clock())
    tz, stream = deps.settings.tz, deps.settings.claude_stream
    data = await _alert_inputs(deps, rng)
    daily_cost = fold_daily(series(data["logs"], "cost"), tz)
    waits = [(t, v / MIN_US) for t, v in series(data["blocked"], "wait_us")]
    ttft_p95 = to_float(data["ttft"][0].get("p95")) if data["ttft"] else None
    api_hourly = [(num(r.get("errors")), num(r.get("events"))) for r in data["logs"]]
    exec_hourly = [(num(r.get("failed")), num(r.get("execs"))) for r in data["exec"]]
    drafts = [
        cost_alert(stream, list(daily_cost.values()), window),
        api_error_alert(stream, api_hourly, window),
        ttft_alert(stream, ttft_p95, [v for _, v in series(data["ttft_hourly"], "p95")], window),
        tool_failure_alert(stream, exec_hourly, window),
        permission_wait_alert(stream, list(fold_daily(waits, tz).values()), window),
        mcp_alert(
            stream,
            [v for _, v in series(data["mcp"], "failures")],
            window,
            "mcp" not in data["errors"],
        ),
    ]
    return _render_alerts(window, drafts, data["errors"])


def _render_alerts(window: str, drafts: list[dict[str, Any]], errors: dict[str, str]) -> ToolOutput:
    rows = [
        [
            d["alert"]["name"],
            d["rationale"]["signal"],
            d["rationale"]["threshold"],
            d["rationale"]["expected_noise"],
        ]
        for d in drafts
    ]
    why = "\n".join(f"- **{d['alert']['name']}** — {d['rationale']['why']}" for d in drafts)
    bodies = "\n".join(json.dumps(d["alert"], separators=(",", ":")) for d in drafts)
    parts = [
        heading(
            f"Recommended alerts from the last {window}",
            "drafts only: nothing was created; enabled=false until reviewed",
        ),
        table(["alert", "signal", "threshold", "expected noise"], rows),
        "### Why\n" + why,
        "### Alert JSON (one per line; import each in OpenObserve → Alerts)\n"
        f"```json\n{bodies}\n```",
        f"_Destination `{ALERT_DESTINATION}` matches the playground's alert files; change it "
        "before importing elsewhere._",
    ]
    parts += [f"_Note: baseline `{k}` unavailable ({v})._" for k, v in errors.items()]
    return ToolOutput(
        "\n\n".join(parts),
        {"window": window, "drafts": drafts, "missing_baselines": sorted(errors)},
    )


# ---------------------------------------------------------------- 6. telemetry health


def stream_flags(now_us: int, last_us: int | None, n_1h: int, n_24h: int) -> list[str]:
    if last_us is None:
        return ["no data in 7d"]
    flags = []
    age_min = (now_us - last_us) / MIN_US
    if age_min > STALE_MIN:
        flags.append(f"stale: last event {round(age_min)} min ago")
    avg = n_24h / 24
    if avg >= DROP_MIN_HOURLY and n_1h < DROP_RATIO * avg:
        flags.append(f"drop: {n_1h} events in the last 1h vs {fmt_num(avg)}/h 24h average")
    return flags


def cross_flags(logs_24h: int, traces_24h: int) -> list[str]:
    if logs_24h > 0 and traces_24h == 0:
        return [
            "missing traces: logs are arriving but there are no spans; the beta tracing flag "
            "(CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 with OTEL_TRACES_EXPORTER) is likely off"
        ]
    if traces_24h > 0 and logs_24h == 0:
        return ["missing logs: spans are arriving but no log events; check OTEL_LOGS_EXPORTER"]
    return []


def content_warning(counts: dict[str, int]) -> str | None:
    hits = {k: v for k, v in counts.items() if v > 0}
    if not hits:
        return None
    where = ", ".join(f"{k}: {v}" for k, v in hits.items())
    return (
        f"COMPLIANCE: prompt/tool content is being captured ({where} rows in 7d). Disable "
        "OTEL_LOG_USER_PROMPTS / OTEL_LOG_TOOL_DETAILS or mask at ingest; these fields can "
        "carry secrets and customer data."
    )


def parse_stream_list(payload: Any, prefix: str, stream_type: str) -> list[StreamRef]:
    """Pick our streams out of GET /api/{org}/streams: {"list": [{name, stats}], "total"}."""
    items = payload.get("list") if isinstance(payload, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name != prefix and not name.startswith(prefix + "_"):
            continue
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        out.append(StreamRef(name, stream_type, to_us(stats.get("doc_time_max")) or None))
    return out


async def _list_streams(deps: Deps) -> tuple[list[StreamRef], list[str]]:
    prefix, notes = deps.settings.claude_stream, []
    found: dict[tuple[str, str], StreamRef] = {}
    for stype in ("logs", "traces", "metrics"):
        try:
            payload = await deps.client.request(
                "GET", "streams", params={"type": stype, "keyword": prefix}
            )
        except O2Error as exc:
            notes.append(f"could not list {stype} streams ({exc})")
            continue
        for ref in parse_stream_list(payload, prefix, stype):
            found[(ref.name, ref.stream_type)] = ref
    for stype in ("logs", "traces"):
        found.setdefault((prefix, stype), StreamRef(prefix, stype))
    refs = sorted(found.values(), key=lambda r: (r.name != prefix, r.stream_type, r.name))
    if len(refs) > MAX_HEALTH_STREAMS:
        notes.append(f"checked the first {MAX_HEALTH_STREAMS} of {len(refs)} streams")
    return refs[:MAX_HEALTH_STREAMS], notes


async def _stream_activity(deps: Deps, ref: StreamRef) -> dict[str, Any]:
    stype, src = cast(StreamType, ref.stream_type), quote_ident(ref.name)
    count = f"SELECT COUNT(*) AS n FROM {src}"
    (h1, e1), (d1, e2), (last, e3) = await asyncio.gather(
        deps.query_optional(count, stype, _range_back(deps, 3600), 1),
        deps.query_optional(count, stype, _range_back(deps, DAY_S), 1),
        deps.query_optional(
            f"SELECT MAX(_timestamp) AS last FROM {src}", stype, _range_back(deps, 7 * DAY_S), 1
        ),
    )
    last_us = (to_us(last[0].get("last")) if last else None) or ref.stats_last_us
    return {
        "stream": ref.name,
        "type": ref.stream_type,
        "last_event_us": last_us,
        "events_1h": int(_first_row(h1, "n")),
        "events_24h": int(_first_row(d1, "n")),
        "queryable": not (e1 or e2 or e3),
    }


async def _content_counts(deps: Deps) -> dict[str, int]:
    c, rng, red = deps.claude, _range_back(deps, 7 * DAY_S), sql_str(REDACTED)
    checks = {
        "traces.user_prompt": (
            f"SELECT COUNT(*) AS n FROM {c} WHERE user_prompt IS NOT NULL AND user_prompt <> {red}",
            "traces",
        ),
        "logs.prompt": (
            f"SELECT COUNT(*) AS n FROM {c} WHERE event_name = 'user_prompt' "
            f"AND prompt IS NOT NULL AND prompt <> {red}",
            "logs",
        ),
        "logs.tool_parameters": (
            f"SELECT COUNT(*) AS n FROM {c} WHERE tool_parameters IS NOT NULL "
            f"AND tool_parameters <> {red}",
            "logs",
        ),
    }
    results = await asyncio.gather(
        *(deps.query_optional(sql, cast(StreamType, st), rng, 1) for sql, st in checks.values())
    )
    return {k: int(_first_row(rows, "n")) for k, (rows, _) in zip(checks, results, strict=True)}


async def telemetry_health(deps: Deps) -> ToolOutput:
    refs, notes = await _list_streams(deps)
    now_us = _now_us(deps)
    stats = list(await asyncio.gather(*(_stream_activity(deps, ref) for ref in refs)))
    content = await _content_counts(deps)
    for s in stats:
        s["flags"] = (
            ["missing: the stream does not exist or cannot be queried"]
            if not s["queryable"] and s["last_event_us"] is None
            else stream_flags(now_us, s["last_event_us"], s["events_1h"], s["events_24h"])
        )
    main = {s["type"]: s["events_24h"] for s in stats if s["stream"] == deps.settings.claude_stream}
    pipeline = cross_flags(main.get("logs", 0), main.get("traces", 0))
    return _render_health(deps, now_us, stats, pipeline, content, notes)


def _render_health(
    deps: Deps,
    now_us: int,
    stats: list[dict[str, Any]],
    pipeline: list[str],
    content: dict[str, int],
    notes: list[str],
) -> ToolOutput:
    tz = deps.settings.tz
    warning = content_warning(content)
    rows = [
        [
            s["stream"],
            s["type"],
            fmt_local(s["last_event_us"], tz),
            None if s["last_event_us"] is None else round((now_us - s["last_event_us"]) / MIN_US),
            s["events_1h"],
            fmt_num(s["events_24h"] / 24),
            "; ".join(s["flags"]) or "ok",
        ]
        for s in stats
    ]
    findings = sum(1 for s in stats if s["flags"]) + len(pipeline) + int(warning is not None)
    parts = [
        heading("Telemetry health", "observability of the observability"),
        "**HEALTHY**" if not findings else f"**{findings} FINDING(S)**",
        table(["stream", "type", "last event", "age min", "1h", "24h avg/h", "flags"], rows),
    ]
    parts += [f"- **{p}**" for p in pipeline]
    if warning:
        parts.append(f"> **{warning}**")
    parts.append(
        "_Stale/drop flags also fire when nobody is using the agent; read them against working "
        "hours. Content checks count rows only, never values._"
    )
    parts += [f"_Note: {n}_" for n in notes]
    data = {
        "streams": stats,
        "pipeline_flags": pipeline,
        "content_capture": content,
        "compliance_warning": warning,
        "notes": notes,
    }
    return ToolOutput("\n\n".join(parts), data)


# ---------------------------------------------------------------- 7. rate limits


def backoff_s(attempt: Any) -> float:
    """Assumed client backoff before retry `attempt` (exponential, capped)."""
    n = max(1, int(num(attempt) or 1))
    return min(BACKOFF_BASE_S * 2 ** (n - 1), BACKOFF_CAP_S)


def rate_limit_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_hour: Counter[int] = Counter()
    by_model: dict[str, Counter[str]] = {}
    attempt_s = backoff_total = 0.0
    total = 0
    for row in rows:
        n = int(num(row.get("n")))
        ts = to_us(row.get("t"))
        if ts is not None:
            by_hour[ts] += n
        model = str(row.get("model") or "(unknown)")
        by_model.setdefault(model, Counter())[str(row.get("status") or "(none)")] += n
        backoff_total += n * backoff_s(row.get("attempt"))
        attempt_s += num(row.get("dur_ms")) / 1000
        total += n
    return {
        "total": total,
        "by_hour": sorted(by_hour.items()),
        "by_model": {m: dict(c) for m, c in by_model.items()},
        "attempt_time_s": round(attempt_s, 1),
        "backoff_time_s": round(backoff_total, 1),
        "time_lost_s": round(attempt_s + backoff_total, 1),
    }


async def _rate_limit_inputs(deps: Deps, rng: TimeRange) -> tuple[list, list, int, list[str]]:
    c, codes = deps.claude, in_list(RATE_LIMIT_CODES)
    status = "COALESCE(CAST(status_code AS VARCHAR), '(none)')"
    head = "SELECT histogram(_timestamp, '1 hour') AS t, COALESCE(model, '(unknown)') AS model, "
    where = f"FROM {c} WHERE event_name = {sql_str(API_ERROR)} AND "
    err = "LOWER(CAST(error AS VARCHAR))"
    variants = [
        f"{head}{status} AS status, TRY_CAST(attempt AS BIGINT) AS attempt, COUNT(*) AS n, "
        f"SUM(TRY_CAST(duration_ms AS DOUBLE)) AS dur_ms {where}"
        f"(CAST(status_code AS VARCHAR) IN ({codes}) OR {err} LIKE '%overloaded%' "
        f"OR {err} LIKE '%rate_limit%') GROUP BY t, model, status, attempt ORDER BY t LIMIT 10000",
        f"{head}{status} AS status, 0 AS attempt, COUNT(*) AS n, 0 AS dur_ms {where}"
        f"CAST(status_code AS VARCHAR) IN ({codes}) GROUP BY t, model, status ORDER BY t "
        "LIMIT 10000",
    ]
    exhausted_sql = (
        f"SELECT COALESCE(model, '(unknown)') AS model, COUNT(*) AS n FROM {c} "
        f"WHERE event_name = {sql_str(RETRIES_EXHAUSTED)} GROUP BY model ORDER BY n DESC LIMIT 50"
    )
    requests_sql = f"SELECT COUNT(*) AS n FROM {c} WHERE event_name = 'api_request'"
    (rl, n1), (ex, n2), (req, n3) = await asyncio.gather(
        _first_ok(deps, variants, "logs", rng, 10000),
        deps.query_optional(exhausted_sql, "logs", rng, 50),
        deps.query_optional(requests_sql, "logs", rng, 1),
    )
    return rl, ex, int(_first_row(req, "n")), [n for n in (n1, n2, n3) if n]


async def rate_limit_report(deps: Deps, window: str = "7d") -> ToolOutput:
    rng = window_range(window, deps.clock())
    rows, exhausted, requests, notes = await _rate_limit_inputs(deps, rng)
    summary = rate_limit_summary(rows)
    ex = [{"model": str(r.get("model")), "count": int(num(r.get("n")))} for r in exhausted]
    if not summary["total"] and not ex:
        return empty(f"Rate limits, last {window}", "No 429/529/overloaded errors.")
    return _render_rate_limits(deps, window, summary, ex, requests, notes)


def _render_rate_limits(
    deps: Deps,
    window: str,
    summary: dict[str, Any],
    exhausted: list[dict[str, Any]],
    requests: int,
    notes: list[str],
) -> ToolOutput:
    tz = deps.settings.tz
    attempts = summary["total"] + requests
    share = round(100 * summary["total"] / attempts, 2) if requests else None
    peaks = sorted(summary["by_hour"], key=lambda kv: kv[1], reverse=True)[:10]
    statuses = sorted({s for c in summary["by_model"].values() for s in c})
    model_rows = [
        [m, sum(c.values()), *[c.get(s, 0) for s in statuses]]
        for m, c in sorted(summary["by_model"].items(), key=lambda kv: -sum(kv[1].values()))
    ]
    parts = [
        heading(f"Rate limits and overload, last {window}", f"hours in {deps.settings.tz_name}"),
        f"**{summary['total']} rate-limited/overloaded API errors** "
        f"({'–' if share is None else share}% of API attempts), "
        f"{sum(e['count'] for e in exhausted)} retries exhausted, "
        f"≈{fmt_num(summary['time_lost_s'] / 60, 1)} min lost to retries.",
        "### By model\n" + table(["model", "total", *statuses], model_rows),
        "### Worst hours\n" + table(["hour", "errors"], [[fmt_local(t, tz), n] for t, n in peaks]),
    ]
    if exhausted:
        rows = [[e["model"], e["count"]] for e in exhausted]
        parts.append("### Retries exhausted\n" + table(["model", "count"], rows))
    parts.append(
        "_time lost = duration of the failed attempts + assumed exponential backoff "
        f"({BACKOFF_BASE_S}s × 2^(attempt−1), capped at {BACKOFF_CAP_S:.0f}s); retry-after "
        "headers are not in the telemetry, so treat it as an estimate._"
    )
    parts += [f"_Note: {n}_" for n in notes]
    data = {
        "window": window,
        **summary,
        "by_hour": [{"hour": fmt_local(t, tz), "errors": n} for t, n in summary["by_hour"]],
        "retries_exhausted": exhausted,
        "api_requests": requests,
        "rate_limited_share_pct": share,
        "notes": notes,
    }
    return ToolOutput("\n\n".join(parts), data)
