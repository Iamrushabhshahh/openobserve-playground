from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import NOW_S, TEST_ENV, FakeO2, load_fixture

from agent_obs_mcp import sre_tools as sre
from agent_obs_mcp.client import O2Client
from agent_obs_mcp.config import load_settings
from agent_obs_mcp.deps import Deps
from agent_obs_mcp.server import build_server
from agent_obs_mcp.sqlguard import CONTENT_COLUMNS

pytestmark = pytest.mark.anyio

NOW_US = int(NOW_S * 1_000_000)
MIN = 60 * 1_000_000
HOUR = 60 * MIN
DAY = 24 * HOUR
IST = load_settings(TEST_ENV).tz
ALERTS_DIR = Path(__file__).resolve().parents[2] / "claude-code" / "alerts"
SRE_TOOLS = {
    "agent_slo_report",
    "regression_check",
    "incident_timeline",
    "budget_forecast",
    "recommend_alerts",
    "telemetry_health",
    "rate_limit_report",
}

Route = tuple[str, Callable[[dict[str, Any]], bool] | None, list[dict[str, Any]]]


class RangeFake(FakeO2):
    """FakeO2 that can also match on the query's time range and answers GET /streams."""

    def __init__(self, routes: list[Route], streams: dict[str, Any] | None = None):
        super().__init__([])
        self.ranged = routes
        self.streams = streams or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            stype = request.url.params.get("type")
            return httpx.Response(200, json=self.streams.get(stype, {"list": [], "total": 0}))
        query = {"type": request.url.params.get("type"), **json.loads(request.content)["query"]}
        self.requests.append(query)
        for needle, pred, hits in self.ranged:
            if needle in query["sql"] and (pred is None or pred(query)):
                return httpx.Response(200, json={"hits": hits, "total": len(hits)})
        return httpx.Response(200, json={"hits": [], "total": 0})


def ranged_deps(
    routes: list[Route], streams: dict[str, Any] | None = None
) -> tuple[Deps, RangeFake]:
    fake = RangeFake(routes, streams)
    settings = load_settings(TEST_ENV)
    client = O2Client(settings, httpx.MockTransport(fake))
    return Deps(settings=settings, client=client, clock=lambda: NOW_S), fake


def span_s(seconds: int) -> Callable[[dict[str, Any]], bool]:
    return lambda q: q["end_time"] - q["start_time"] == seconds * 1_000_000


def ends_now(q: dict[str, Any]) -> bool:
    return q["end_time"] == NOW_US


# ---------------------------------------------------------------- shared helpers


def test_to_us_parses_histogram_buckets_and_epochs() -> None:
    assert sre.to_us("2026-10-08T12:00:00") == NOW_US
    assert sre.to_us("2026-10-08T12:00:00Z") == NOW_US
    assert sre.to_us(NOW_US) == NOW_US
    assert sre.to_us(NOW_US * 1000) == NOW_US
    assert sre.to_us(int(NOW_S)) == NOW_US
    assert sre.to_us(str(NOW_US)) == NOW_US
    assert sre.to_us("not a time") is None


def test_quantile_and_mad() -> None:
    assert sre.quantile([1, 2, 3, 4], 0.5) == 2.5
    assert sre.quantile([], 0.5) is None
    assert sre.mad([1, 2, 3, 4, 100]) == 1


# ---------------------------------------------------------------- 1. SLO / burn rate


def test_burn_rate_math() -> None:
    assert sre.burn_rate(5, 100, 0.95) == pytest.approx(1.0)
    assert sre.burn_rate(0, 0, 0.95) is None
    assert sre.budget_remaining_pct(5, 100, 0.95) == 0.0
    assert sre.budget_remaining_pct(1, 100, 0.95) == 80.0
    assert sre.budget_remaining_pct(10, 100, 0.95) == -100.0


def _series_with_fast_burn() -> list[sre.SliPoint]:
    """72h of one event per minute: 1% bad, except 90% bad in the last hour."""
    points = []
    for i in range(72 * 60):
        ts = NOW_US - i * MIN
        bad = i % 10 != 0 if i < 60 else i % 100 == 0
        points.append(sre.SliPoint(ts, 1, int(bad)))
    return points


def test_multiwindow_burn_fires_only_fast_page() -> None:
    rules = {r["long"]: r for r in sre.evaluate_burn(_series_with_fast_burn(), NOW_US, 0.95)}
    assert rules["1h"]["long_burn"] == round(54 / 61 / 0.05, 2)
    assert rules["1h"]["short_burn"] == round(5 / 6 / 0.05, 2)
    assert rules["1h"]["fired"] and rules["1h"]["action"] == "page"
    assert not rules["6h"]["fired"]
    assert rules["6h"]["long_burn"] == round(57 / 361 / 0.05, 2)
    assert not rules["1d"]["fired"]
    assert not rules["3d"]["fired"]
    verdict = sre.slo_verdict({"interaction_success": list(rules.values())}, {"x": 50.0}, True)
    assert verdict.startswith("PAGE") and "1h+5m" in verdict


def test_burn_needs_both_windows() -> None:
    points = [sre.SliPoint(NOW_US - 40 * MIN - i * MIN, 1, 1) for i in range(20)]
    points += [sre.SliPoint(NOW_US - i * MIN, 1, 0) for i in range(5)]
    rule = sre.evaluate_burn(points, NOW_US, 0.95)[0]
    assert rule["long_burn"] >= 14.4
    assert rule["short_burn"] == 0.0
    assert not rule["fired"]


def test_slo_verdicts_without_burn() -> None:
    quiet = {"interaction_success": [{"fired": False, "action": "page"}]}
    assert sre.slo_verdict(quiet, {"a": -5.0}, True).startswith("SLO MISSED")
    assert sre.slo_verdict(quiet, {"a": 40.0}, False).startswith("SLO MISSED")
    assert sre.slo_verdict(quiet, {"a": 40.0, "b": 90.0}, True).endswith("a 40.0%")
    assert sre.slo_verdict(quiet, {"a": None}, None).startswith("NO DATA")


def test_classify_interactions_reasons() -> None:
    rows = [
        {"trace_id": "t1", "_timestamp": NOW_US - MIN, "duration": 10e6},
        {"trace_id": "t2", "_timestamp": NOW_US - MIN, "duration": 400e6},
        {"trace_id": "t3", "_timestamp": NOW_US - MIN, "duration": 10e6},
        {"trace_id": "t4", "_timestamp": NOW_US - MIN, "duration": 10e6},
    ]
    points, reasons = sre.classify_interactions(rows, {"t3"}, {"t3", "t4"}, 300e6)
    assert [p.bad for p in points] == [0, 1, 1, 1]
    assert reasons == {"failed_tool": 1, "api_error": 2, "slow": 1}


async def test_agent_slo_report_end_to_end() -> None:
    inter = [
        {"trace_id": f"t{i}", "_timestamp": NOW_US - (i + 1) * HOUR, "duration": 60e6}
        for i in range(40)
    ]
    avail = [{"t": NOW_US - (i + 1) * HOUR, "ok": 99, "errors": 1} for i in range(40)]
    deps, fake = ranged_deps(
        [
            ("operation_name = 'claude_code.interaction'", None, inter),
            ("claude_code.tool.execution", None, [{"trace_id": "t0", "n": 1}]),
            ("AND trace_id IS NOT NULL GROUP BY trace_id", None, [{"trace_id": "t1", "n": 2}]),
            ("'5 minute'", None, avail),
        ]
    )
    out = await sre.agent_slo_report(deps, "7d", 0.95, 300, 0.95)
    slis = out.data["slis"]
    assert slis["interaction_success"]["bad"] == 2
    assert slis["interaction_success"]["total"] == 40
    assert slis["interaction_success"]["budget_remaining_pct"] == 0.0
    assert slis["llm_availability"]["good_ratio"] == 0.99
    assert slis["llm_availability"]["budget_remaining_pct"] == 80.0
    assert slis["interaction_latency"]["value_s"] == 60.0
    assert out.data["verdict"].startswith("OK")
    assert "**Verdict:" in out.markdown
    assert {r["type"] for r in fake.requests} == {"traces", "logs"}
    assert all(q["end_time"] - q["start_time"] == 7 * DAY for q in fake.requests)


async def test_agent_slo_report_rejects_bad_target(make_deps) -> None:
    deps, fake = make_deps()
    with pytest.raises(ValueError):
        await sre.agent_slo_report(deps, success_target=1.0)
    assert fake.requests == []


# ---------------------------------------------------------------- 2. regression


def _ttft_rows(start_us: int, n: int, version: str, base_ms: float) -> list[dict[str, Any]]:
    return [
        {
            "_timestamp": start_us + i * MIN,
            "trace_id": f"{version}-{i}",
            "v": base_ms + (i % 10) * 20,
            "version": version,
            "model": "claude-sonnet-4-5",
        }
        for i in range(n)
    ]


async def test_regression_detects_and_attributes_new_version() -> None:
    base = _ttft_rows(NOW_US - 5 * DAY, 40, "2.0.10", 900)
    recent = _ttft_rows(NOW_US - 10 * HOUR, 30, "2.0.11", 1800)
    recent += _ttft_rows(NOW_US - 20 * HOUR, 10, "2.0.10", 900)
    deps, fake = ranged_deps(
        [
            ("TRY_CAST(ttft_ms AS DOUBLE) AS v", ends_now, recent),
            ("TRY_CAST(ttft_ms AS DOUBLE) AS v", None, base),
        ]
    )
    out = await sre.regression_check(deps, "ttft", "7d", "24h")
    assert out.data["verdict"] == "regression"
    assert out.data["z"] >= 3
    assert out.data["change_pct"] > 50
    cause = out.data["likely_cause"]
    assert cause["kind"] == "deploy marker"
    assert cause["dimension"] == "version" and cause["group"] == "2.0.11"
    assert "held steady" in cause["text"]
    versions = {r["version"]: r for r in out.data["by_version"]}
    assert versions["2.0.11"]["new"] and not versions["2.0.10"]["new"]
    assert "service_service_version AS version" in fake.sqls()[0]
    base_q = next(q for q in fake.requests if q["end_time"] != NOW_US)
    assert base_q["end_time"] == NOW_US - DAY
    assert base_q["end_time"] - base_q["start_time"] == 7 * DAY


def test_regression_without_deploy_marker() -> None:
    mk = sre.Sample
    base = [mk(i, 100 + i % 5, "1.0", "opus") for i in range(50)]
    recent = [mk(10_000 + i, 150 + i % 5, "1.0", "opus") for i in range(50)]
    result = sre.compare([s.value for s in base], [s.value for s in recent], False)
    assert result["verdict"] == "regression"
    dims = {d: sre.breakdown(base, recent, d, False) for d in ("version", "model")}
    cause = sre.likely_cause(dims, result["change_pct"], IST)
    assert cause["kind"] == "none"


def test_model_switch_attributed_as_deploy_marker() -> None:
    mk = sre.Sample
    base = [mk(i, 100 + i % 5, "1.0", "sonnet") for i in range(50)]
    recent = [mk(10_000 + i, 300 + i % 5, "1.0", "opus") for i in range(40)]
    recent += [mk(20_000 + i, 100 + i % 5, "1.0", "sonnet") for i in range(10)]
    result = sre.compare([s.value for s in base], [s.value for s in recent], False)
    dims = {d: sre.breakdown(base, recent, d, False) for d in ("version", "model")}
    cause = sre.likely_cause(dims, result["change_pct"], IST)
    assert (cause["dimension"], cause["group"]) == ("model", "opus")


def test_failure_rate_uses_two_proportion_z() -> None:
    base = [1.0] * 5 + [0.0] * 95
    recent = [1.0] * 30 + [0.0] * 70
    result = sre.compare(base, recent, True)
    assert result["verdict"] == "regression"
    assert result["baseline"]["rate_pct"] == 5.0 and result["recent"]["rate_pct"] == 30.0
    assert sre.proportion_z(5, 100, 5, 100) == 0
    assert sre.compare(base[:5], recent[:5], True)["verdict"] == "insufficient data"


async def test_regression_rejects_unknown_metric(make_deps) -> None:
    deps, _ = make_deps()
    with pytest.raises(ValueError):
        await sre.regression_check(deps, "vibes")


# ---------------------------------------------------------------- 3. incident timeline


INCIDENT_ROUTES: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "event_name IN ('api_error', 'api_retries_exhausted') ORDER BY",
        [
            {
                "_timestamp": NOW_US - 90 * MIN,
                "event_name": "api_error",
                "session_id": "s1",
                "trace_id": "t1",
                "model": "opus",
                "status": "529",
            },
            {
                "_timestamp": NOW_US - 89 * MIN,
                "event_name": "api_error",
                "session_id": "s1",
                "trace_id": "t1",
                "model": "opus",
                "status": "529",
            },
            {
                "_timestamp": NOW_US - 87 * MIN,
                "event_name": "api_retries_exhausted",
                "session_id": "s1",
                "trace_id": "t1",
                "model": "opus",
            },
        ],
    ),
    (
        "mcp_server_connection",
        [{"_timestamp": NOW_US - 50 * MIN, "session_id": "s2", "server": "github"}],
    ),
    ("span_id IN", [{"span_id": "p1", "tool_name": "Bash"}]),
    (
        "claude_code.tool.execution",
        [
            {
                "_timestamp": NOW_US - 100 * MIN,
                "trace_id": "t0",
                "session_id": "s3",
                "tool_name": None,
                "error_class": "ExitCode",
                "reference_parent_span_id": "p1",
            }
        ],
    ),
    ("COUNT(DISTINCT session_id)", [{"sessions": 6}]),
    (
        "'5 minute'",
        [
            {"t": "2026-10-08T10:30:00", "p95": 9000, "n": 5},
            {"t": "2026-10-08T10:35:00", "p95": 2500, "n": 5},
        ],
    ),
    ("approx_percentile_cont", [{"p95": 2000}]),
]


def test_collapse_merges_bursts() -> None:
    evs = [
        sre.TimelineEvent(0, "api_error", "529"),
        sre.TimelineEvent(MIN, "api_error", "529"),
        sre.TimelineEvent(30 * MIN, "api_error", "529"),
        sre.TimelineEvent(2 * MIN, "tool_failure", "Bash"),
    ]
    runs = sre.collapse(evs)
    assert [(r["kind"], r["count"]) for r in runs] == [
        ("api_error", 2),
        ("tool_failure", 1),
        ("api_error", 1),
    ]


async def test_incident_timeline_orders_and_finds_first_bad(make_deps) -> None:
    deps, fake = make_deps(INCIDENT_ROUTES)
    out = await sre.incident_timeline(deps, window="2h")
    data = out.data
    assert data["first_bad"]["kind"] == "tool_failure"
    assert data["first_bad"]["detail"] == "Bash failed (ExitCode)"
    assert data["last_bad"]["kind"] == "mcp_failure"
    times = [r["first_us"] for r in data["timeline"]]
    assert times == sorted(times)
    kinds = [r["kind"] for r in data["timeline"]]
    assert kinds == [
        "tool_failure",
        "api_error",
        "latency_spike",
        "retries_exhausted",
        "mcp_failure",
    ]
    assert data["timeline"][1]["count"] == 2
    assert data["by_kind"] == {
        "tool_failure": 1,
        "api_error": 2,
        "latency_spike": 1,
        "retries_exhausted": 1,
        "mcp_failure": 1,
    }
    assert data["affected_sessions"] == ["s1", "s2", "s3"]
    assert data["active_sessions"] == 6
    assert any("overlapped API errors" in f for f in data["contributing_factors"])
    assert all(q.endswith("?") for q in data["follow_ups"])
    assert "postmortem draft (blameless)" in out.markdown
    assert "2026-10-08 15:50:00" in out.markdown
    spike_q = next(
        r
        for r in fake.requests
        if "'5 minute'" not in r["sql"] and "approx_percentile_cont" in r["sql"]
    )
    assert spike_q["end_time"] == NOW_US - 2 * HOUR


def test_incident_range_parsing() -> None:
    rng = sre.incident_range("2026-10-08T15:00", None, "2h", NOW_S, IST)
    assert rng.start_us == NOW_US - int(2.5 * HOUR)
    assert rng.end_us == NOW_US - int(0.5 * HOUR)
    rng = sre.incident_range("2026-10-08T17:00", "2026-10-08T18:00+05:30", "2h", NOW_S, IST)
    assert rng.end_us - rng.start_us == HOUR
    with pytest.raises(ValueError):
        sre.incident_range("2026-10-08T18:00", "2026-10-08T17:00", "2h", NOW_S, IST)
    with pytest.raises(ValueError):
        sre.incident_range("yesterday", None, "2h", NOW_S, IST)
    with pytest.raises(ValueError):
        sre.incident_range("2026-09-01T00:00", "2026-09-20T00:00", "2h", NOW_S, IST)


async def test_incident_timeline_no_events(make_deps) -> None:
    deps, _ = make_deps()
    out = await sre.incident_timeline(deps)
    assert out.data["events"] == 0 and "No bad events" in out.markdown


# ---------------------------------------------------------------- 4. budget forecast


def test_forecast_math_with_fixed_clock() -> None:
    now, start, end = sre.month_bounds(NOW_S, IST)
    assert now.isoformat() == "2026-10-08T17:30:00+05:30"
    assert start.isoformat() == "2026-10-01T00:00:00+05:30"
    assert end.isoformat() == "2026-11-01T00:00:00+05:30"
    fc = sre.forecast_budget(300, 100, 10, now, end)
    days_left = (23 * 24 + 6.5) / 24
    assert fc["days_left"] == round(days_left, 2)
    assert fc["projected_month_end_usd"] == round(100 + 10 * days_left, 2)
    assert fc["exhaustion_date"] == "2026-10-28"
    assert fc["status"] == "exhausts before month end"
    assert fc["daily_cap_to_land_on_budget_usd"] == round(200 / days_left, 2)
    assert "Over budget" in sre.cap_recommendation(fc)
    calm = sre.forecast_budget(1000, 100, 10, now, end)
    assert calm["status"] == "within budget" and "On track" in sre.cap_recommendation(calm)
    assert sre.forecast_budget(50, 100, 10, now, end, "2026-10-05")["exhaustion_date"] == (
        "2026-10-05"
    )


def test_crossing_date_uses_local_days() -> None:
    late_utc = int(dt.datetime(2026, 10, 3, 20, 0, tzinfo=dt.UTC).timestamp() * 1e6)
    points = [(late_utc - DAY, 30.0), (late_utc, 30.0)]
    assert sre.crossing_date(points, 50, IST) == "2026-10-04"
    assert sre.crossing_date(points, 100, IST) is None


async def test_budget_forecast_end_to_end(make_deps) -> None:
    mtd = [{"t": "2026-10-02T06:00:00", "cost": 60}, {"t": "2026-10-07T06:00:00", "cost": "40"}]
    deps, fake = make_deps(
        [
            ("histogram(_timestamp, '1 hour')", mtd),
            ("COUNT(*) AS calls", [{"cost": 70.0, "calls": 900}]),
            ("COUNT(DISTINCT trace_id)", [{"n": 10}]),
            ("operation_name = 'claude_code.interaction'", [{"n": 50}]),
            ("GROUP BY trace_id", [{"trace_id": "t9", "cost": 4.5}]),
            ("GROUP BY session_id", [{"session_id": "s9", "cost": 12.25}]),
        ]
    )
    out = await sre.budget_forecast(deps, 300, "7d")
    d = out.data
    assert d["spent_mtd_usd"] == 100.0
    assert d["daily_run_rate_usd"] == 10.0
    assert d["exhaustion_date"] == "2026-10-28"
    assert d["successful_interactions"] == 40
    assert d["cost_per_successful_interaction_usd"] == 1.75
    assert d["top_traces"] == [{"trace_id": "t9", "cost_usd": 4.5}]
    assert d["top_sessions"] == [{"session_id": "s9", "cost_usd": 12.25}]
    month_q = next(r for r in fake.requests if "'1 hour'" in r["sql"])
    month_start = dt.datetime(2026, 10, 1, tzinfo=IST).timestamp() * 1_000_000
    assert month_q["start_time"] == int(month_start)
    assert "Asia/Kolkata" in out.markdown


async def test_budget_forecast_rejects_non_positive(make_deps) -> None:
    deps, _ = make_deps()
    with pytest.raises(ValueError):
        await sre.budget_forecast(deps, 0)


# ---------------------------------------------------------------- 5. alerts


def _key_shape(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _key_shape(v) for k, v in obj.items() if not isinstance(v, list)} | {
            k: "list" for k, v in obj.items() if isinstance(v, list)
        }
    return type(obj).__name__ if obj is not None else "null"


def _repo_alert_shapes() -> list[Any]:
    files = sorted(ALERTS_DIR.glob("*.json"))
    if not files:
        pytest.skip("claude-code/alerts not found next to the mcp package")
    return [_key_shape(json.loads(f.read_text())) for f in files]


async def test_recommend_alerts_shapes_match_repo_alerts(make_deps) -> None:
    hours = [
        {"t": NOW_US - (24 * d + 2) * HOUR, "cost": 10 + d, "events": 100, "errors": d % 3}
        for d in range(10)
    ]
    execs = [{"t": NOW_US - h * HOUR, "execs": 50, "failed": 2 + h % 4} for h in range(1, 30)]
    blocked = [{"t": NOW_US - (24 * d + 3) * HOUR, "wait_us": 6e8} for d in range(5)]
    deps, _ = make_deps(
        [
            ("AS errors FROM", hours),
            ("AS failed FROM", execs),
            ("AS wait_us", blocked),
            ("'1 hour') AS t, approx_percentile_cont", [{"t": NOW_US - HOUR, "p95": 9000}]),
            ("SELECT approx_percentile_cont", [{"p95": 3000}]),
            ("AS failures FROM", [{"t": NOW_US - HOUR, "failures": 1}]),
        ]
    )
    out = await sre.recommend_alerts(deps, "14d")
    drafts = out.data["drafts"]
    assert len(drafts) == 6
    reference = _repo_alert_shapes()[0]
    for draft in drafts:
        alert = json.loads(json.dumps(draft["alert"]))
        assert _key_shape(alert) == reference
        assert alert["stream_name"] == "claude_code"
        assert alert["stream_type"] in {"logs", "traces"}
        assert 'FROM "claude_code"' in alert["query_condition"]["sql"]
        assert "HAVING" in alert["query_condition"]["sql"]
        assert alert["enabled"] is False
        assert set(draft["rationale"]) == {"signal", "threshold", "why", "expected_noise"}
    by_name = {d["alert"]["name"]: d for d in drafts}
    daily = [10 + d for d in range(10)]
    cost_limit = round(sre.quantile(daily, 0.95) * 1.5, 2)
    assert (
        f"> {cost_limit}"
        in by_name["claude_code_slo_daily_cost"]["alert"]["query_condition"]["sql"]
    )
    assert "> 6000" in by_name["claude_code_slo_ttft_p95"]["alert"]["query_condition"]["sql"]
    assert (
        "would have fired 1×" in by_name["claude_code_slo_ttft_p95"]["rationale"]["expected_noise"]
    )
    wait = by_name["claude_code_slo_permission_wait"]["rationale"]["threshold"]
    assert wait == "> 15.0 min"
    assert "```json" in out.markdown


def test_alert_defaults_without_baseline() -> None:
    draft = sre.cost_alert("claude_code", [], "14d")
    assert "> 1.0" in draft["alert"]["query_condition"]["sql"]
    assert draft["rationale"]["expected_noise"].startswith("no baseline")
    assert sre.mcp_alert("claude_code", [], "14d", True)["rationale"]["threshold"] == "≥ 3"


# ---------------------------------------------------------------- 6. telemetry health


def test_stream_flags() -> None:
    assert sre.stream_flags(NOW_US, NOW_US - 5 * MIN, 50, 24 * 50) == []
    stale = sre.stream_flags(NOW_US, NOW_US - 45 * MIN, 50, 24 * 50)
    assert len(stale) == 1 and stale[0].startswith("stale")
    drop = sre.stream_flags(NOW_US, NOW_US - MIN, 10, 24 * 50)
    assert len(drop) == 1 and drop[0].startswith("drop")
    assert sre.stream_flags(NOW_US, NOW_US - MIN, 0, 24) == []
    assert sre.stream_flags(NOW_US, None, 0, 0) == ["no data in 7d"]


def test_cross_and_content_flags() -> None:
    assert "beta tracing" in sre.cross_flags(100, 0)[0]
    assert sre.cross_flags(0, 10)[0].startswith("missing logs")
    assert sre.cross_flags(100, 10) == []
    assert sre.content_warning({"logs.prompt": 0}) is None
    assert "COMPLIANCE" in (sre.content_warning({"logs.prompt": 3}) or "")


def test_parse_stream_list_matches_openobserve_shape() -> None:
    payload = load_fixture("sre_streams_metrics.json")
    refs = sre.parse_stream_list(payload, "claude_code", "metrics")
    assert [r.name for r in refs] == ["claude_code_cost_usage", "claude_code_token_usage"]
    assert refs[0].stats_last_us == NOW_US - 10 * MIN
    assert sre.parse_stream_list({"oops": 1}, "claude_code", "logs") == []


async def test_telemetry_health_end_to_end() -> None:
    hr, day, week = span_s(3600), span_s(86_400), span_s(7 * 86_400)
    logs = lambda q: q["type"] == "logs"  # noqa: E731
    deps, fake = ranged_deps(
        [
            ("user_prompt IS NOT NULL", None, [{"n": 3}]),
            ("IS NOT NULL", None, [{"n": 0}]),
            ('FROM "claude_code"', lambda q: logs(q) and hr(q), [{"n": 0}]),
            ('FROM "claude_code"', lambda q: logs(q) and day(q), [{"n": 240}]),
            ("MAX(_timestamp)", lambda q: logs(q) and week(q), [{"last": NOW_US - 2 * HOUR}]),
            ('FROM "claude_code_cost_usage"', hr, [{"n": 30}]),
            ('FROM "claude_code_cost_usage"', day, [{"n": 720}]),
            (
                'MAX(_timestamp) AS last FROM "claude_code_cost_usage"',
                None,
                [{"last": NOW_US - 5 * MIN}],
            ),
        ],
        streams={"metrics": load_fixture("sre_streams_metrics.json")},
    )
    out = await sre.telemetry_health(deps)
    streams = {(s["stream"], s["type"]): s for s in out.data["streams"]}
    flags = streams[("claude_code", "logs")]["flags"]
    assert any(f.startswith("stale: last event 120 min") for f in flags)
    assert any(f.startswith("drop") for f in flags)
    assert streams[("claude_code", "traces")]["flags"] == ["no data in 7d"]
    assert streams[("claude_code_cost_usage", "metrics")]["flags"] == []
    assert streams[("claude_code_token_usage", "metrics")]["last_event_us"] == NOW_US - 3 * HOUR
    assert "beta tracing" in out.data["pipeline_flags"][0]
    assert out.data["content_capture"]["traces.user_prompt"] == 3
    assert "COMPLIANCE" in out.data["compliance_warning"]
    assert "FINDING" in out.markdown
    assert {r["type"] for r in fake.requests} == {"logs", "traces", "metrics"}


# ---------------------------------------------------------------- 7. rate limits


def test_rate_limit_summary_and_backoff() -> None:
    assert sre.backoff_s(1) == 0.5 and sre.backoff_s(3) == 2.0 and sre.backoff_s(20) == 32.0
    assert sre.backoff_s(None) == 0.5
    rows = [
        {
            "t": NOW_US - HOUR,
            "model": "opus",
            "status": "529",
            "attempt": 1,
            "n": 4,
            "dur_ms": 2000,
        },
        {
            "t": NOW_US - HOUR,
            "model": "opus",
            "status": "529",
            "attempt": 2,
            "n": 2,
            "dur_ms": 1000,
        },
        {
            "t": NOW_US - 2 * HOUR,
            "model": "haiku",
            "status": "429",
            "attempt": 1,
            "n": 1,
            "dur_ms": 0,
        },
    ]
    s = sre.rate_limit_summary(rows)
    assert s["total"] == 7
    assert s["by_model"] == {"opus": {"529": 6}, "haiku": {"429": 1}}
    assert s["by_hour"] == [(NOW_US - 2 * HOUR, 1), (NOW_US - HOUR, 6)]
    assert s["backoff_time_s"] == 4 * 0.5 + 2 * 1.0 + 0.5
    assert s["time_lost_s"] == 3.0 + 4.5


async def test_rate_limit_report_end_to_end(make_deps) -> None:
    rl = [
        {
            "t": "2026-10-08T11:00:00",
            "model": "opus",
            "status": "529",
            "attempt": 1,
            "n": 9,
            "dur_ms": 900,
        }
    ]
    deps, fake = make_deps(
        [
            ("api_retries_exhausted", [{"model": "opus", "n": 2}]),
            ("event_name = 'api_error'", rl),
            ("event_name = 'api_request'", [{"n": 91}]),
        ]
    )
    out = await sre.rate_limit_report(deps, "7d")
    assert out.data["total"] == 9
    assert out.data["rate_limited_share_pct"] == 9.0
    assert out.data["retries_exhausted"] == [{"model": "opus", "count": 2}]
    assert out.data["by_hour"] == [{"hour": "2026-10-08 16:30", "errors": 9}]
    assert "'429', '529'" in fake.sqls()[0]


# ---------------------------------------------------------------- privacy and wiring


_CONTENT_RE = re.compile(r"\b(" + "|".join(sorted(CONTENT_COLUMNS)) + r")\b", re.IGNORECASE)
_ALLOWED_CONTENT_USE = re.compile(
    r"\b(\w+) IS NOT NULL|\b(\w+) <> '<REDACTED>'|event_name = 'user_prompt'"
)


def _assert_no_content_selected(sql: str) -> None:
    select_list = re.split(r"\bFROM\b", sql, maxsplit=1)[0]
    assert not _CONTENT_RE.search(select_list), sql
    residue = _ALLOWED_CONTENT_USE.sub("", sql.split("FROM", 1)[1] if "FROM" in sql else "")
    assert not _CONTENT_RE.search(residue.replace('"', "")), sql


async def test_content_columns_never_selected() -> None:
    deps, fake = ranged_deps([], streams={"metrics": load_fixture("sre_streams_metrics.json")})
    await sre.agent_slo_report(deps)
    for metric in sre.REGRESSION_METRICS:
        await sre.regression_check(deps, metric)
    await sre.incident_timeline(deps)
    await sre.budget_forecast(deps, 100)
    await sre.recommend_alerts(deps)
    await sre.telemetry_health(deps)
    await sre.rate_limit_report(deps)
    sqls = fake.sqls()
    assert len(sqls) > 40
    for sql in sqls:
        _assert_no_content_selected(sql)
    assert not any("SELECT *" in s for s in sqls)


async def test_tools_registered_read_only() -> None:
    server = build_server(load_settings(TEST_ENV))
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) >= SRE_TOOLS
    for name in SRE_TOOLS:
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].description


async def test_server_call_returns_clean_error_for_bad_input() -> None:
    fake = FakeO2([])
    server = build_server(load_settings(TEST_ENV), httpx.MockTransport(fake), clock=lambda: NOW_S)
    result = await server.call_tool("incident_timeline", {"start": "not-a-date"})
    assert result.isError and "cannot parse time" in result.content[0].text
    assert fake.requests == []
