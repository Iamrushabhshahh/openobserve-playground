"""Agent behaviour: loops, session diffs, audit trail, risky actions, Codex."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from . import traces as tr
from .client import O2Error, StreamType
from .deps import Deps, clamp, in_list, quote_ident, require_id, sql_str
from .render import ToolOutput, empty, fmt_us_ts, heading, num, table, to_float
from .sqlguard import is_content_column
from .timewin import TimeRange, window_range
from .toolkit import READ_ONLY, Window, run_tool

RAW_CAP = 20_000
AUDIT_CAP = 2_000
FAIL_CHAIN_MIN = 2
FLAG_RATIO = 2.0
FAILED = "CAST(success AS VARCHAR) = 'false'"
API_ERROR_EVENTS = ("api_error", "api_retries_exhausted")
AUDIT_EVENTS = (
    "tool_decision",
    "tool_result",
    "mcp_server_connection",
    "permission_mode_changed",
    "user_prompt",
    *API_ERROR_EVENTS,
)
EXPLORATORY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch"})
SAME_TARGET_TOOLS = frozenset({"Read", "Grep", "Glob"})
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
# Arguments/outputs that sqlguard's content list does not cover (Claude Code + Codex names).
GATED_COLUMNS = frozenset(
    {"file_path", "full_command", "bash_command", "arguments", "output", "error", "error_message"}
)
VERIFIED_COLUMNS: dict[str, frozenset[str]] = {
    "traces": frozenset(
        {
            "trace_id",
            "span_id",
            "reference_parent_span_id",
            "operation_name",
            "start_time",
            "end_time",
            "duration",
            "session_id",
            "tool_name",
            "decision",
            "source",
            "success",
            "model",
            "agent_id",
            "error_class",
            "bash_command_class",
            "bash_argv0",
        }
    ),
    "logs": frozenset(
        {
            "_timestamp",
            "event_name",
            "cost_usd",
            "model",
            "tool_name",
            "success",
            "session_id",
            "trace_id",
            "status_code",
            "decision",
            "source",
            "server_name",
            "status",
            "mode",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        }
    ),
}
_PRIV = ("privilege escalation", 5)
_DESTRUCTIVE = ("destructive / system change", 4)
_CLOUD = ("cloud / infra CLI", 4)
_NETWORK = ("network", 3)
_PACKAGES = ("package manager (may install)", 2)
_VCS = ("vcs (may push; subcommand not recorded)", 1)
RISK_ARGV0: dict[str, tuple[str, int]] = {
    **dict.fromkeys(("sudo", "su", "doas"), _PRIV),
    **dict.fromkeys(
        ("rm", "rmdir", "dd", "mkfs", "shred", "truncate", "chmod", "chown", "kill", "pkill"),
        _DESTRUCTIVE,
    ),
    **dict.fromkeys(
        ("aws", "gcloud", "az", "kubectl", "terraform", "helm", "pulumi", "eksctl", "doctl"),
        _CLOUD,
    ),
    **dict.fromkeys(("flyctl", "vercel", "heroku", "docker"), _CLOUD),
    **dict.fromkeys(
        ("curl", "wget", "nc", "ncat", "ssh", "scp", "rsync", "ftp", "telnet"), _NETWORK
    ),
    **dict.fromkeys(
        ("npm", "npx", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "brew", "cargo"),
        _PACKAGES,
    ),
    **dict.fromkeys(("apt", "apt-get", "yum", "dnf", "gem"), _PACKAGES),
    "git": _VCS,
}
# bash_command_class values beyond vcs/package_manager/other/unparsed are guesses (unverified).
RISK_CLASSES: dict[str, tuple[str, int]] = {
    "privileged": _PRIV,
    "sudo": _PRIV,
    "destructive": _DESTRUCTIVE,
    "file_delete": _DESTRUCTIVE,
    "cloud": _CLOUD,
    "infra": _CLOUD,
    "network": _NETWORK,
    "package_manager": _PACKAGES,
    "vcs": _VCS,
}
MODE_RISK = {"bypassPermissions": 5, "auto": 3, "acceptEdits": 2}
SEVERITY = ((5, "critical"), (4, "high"), (3, "medium"), (0, "low"))
COMPARE_METRICS = (
    ("cost_usd", "cost $"),
    ("llm_calls", "LLM calls"),
    ("tool_calls", "tool calls"),
    ("tool_failures", "tool failures"),
    ("api_errors", "API errors"),
    ("permission_wait_min", "permission wait min"),
    ("cache_hit_pct", "cache hit %"),
    ("p95_tool_ms", "p95 tool ms"),
)
CODEX_EVENTS = {
    "api": ("codex.api_request", "api_request"),
    "ws": ("codex.websocket_request", "websocket_request"),
    "sse": ("codex.sse_event", "sse_event"),
    "tool": ("codex.tool_result", "tool_result"),
    "cost": ("codex.turn_cost", "turn_cost"),
}


@dataclass(frozen=True)
class Columns:
    """Which columns a stream really has, so queries never name a missing or gated field."""

    stream_type: StreamType
    fields: frozenset[str] | None
    allow_content: bool

    def has(self, name: str) -> bool:
        gated = is_content_column(name) or name in GATED_COLUMNS
        if gated and not self.allow_content:
            return False
        known = self.fields if self.fields is not None else VERIFIED_COLUMNS[self.stream_type]
        return name in known

    def pick(self, *names: str) -> list[str]:
        return [n for n in names if self.has(n)]


@dataclass
class ToolCall:
    span_id: str
    key: str
    tool: str
    start_ns: int
    seconds: float
    session_id: str = ""
    target: str = ""
    failed: bool = False


@dataclass
class Suspect:
    trace_id: str
    session_id: str
    pattern: str
    detail: str
    count: int
    wasted_s: float
    failures: int = 0
    confidence: str = "medium"

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "pattern": self.pattern,
            "detail": self.detail,
            "count": self.count,
            "wasted_s": round(self.wasted_s, 1),
            "failures": self.failures,
            "confidence": self.confidence,
        }


@dataclass
class Finding:
    category: str
    item: str
    weight: int
    count: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        return next(label for floor, label in SEVERITY if self.weight >= floor)

    def add(self, count: int, example: Any) -> None:
        self.count += count
        if example and str(example) not in self.examples and len(self.examples) < 3:
            self.examples.append(str(example))

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "item": self.item,
            "count": self.count,
            "examples": self.examples,
        }


Findings = dict[tuple[str, str], Finding]
SessionId = Annotated[str, Field(description="session_id of one Claude Code session")]
Collector = Callable[[Deps, TimeRange, Columns, Columns, Findings], Awaitable[str | None]]


def register(mcp: FastMCP, deps: Deps) -> None:
    """Register this module's tools on `mcp`."""
    tool = mcp.tool

    @tool(annotations=READ_ONLY)
    async def detect_loops(
        window: Window = "24h", min_repeats: Annotated[int, Field(ge=2, le=50)] = 3
    ) -> CallToolResult:
        """Stuck-agent suspects: repeated tool calls, fail→retry chains, retry storms."""
        return await run_tool(find_loop_suspects(deps, window, min_repeats))

    @tool(annotations=READ_ONLY)
    async def compare_sessions(
        session_a: SessionId, session_b: SessionId, window: Window = "30d"
    ) -> CallToolResult:
        """Two sessions side by side (cost, calls, failures, waits, cache, p95); flags ≥2×."""
        return await run_tool(compare_two_sessions(deps, session_a, session_b, window))

    @tool(annotations=READ_ONLY)
    async def audit_trail(
        session_id: SessionId, include_commands: bool = False, window: Window = "30d"
    ) -> CallToolResult:
        """Chronological, content-redacted compliance action log of one session."""
        return await run_tool(session_audit_trail(deps, session_id, include_commands, window))

    @tool(annotations=READ_ONLY)
    async def risky_actions(window: Window = "7d") -> CallToolResult:
        """Security review: rejections, aborts, mode escalations, risky Bash classes, MCP."""
        return await run_tool(review_risky_actions(deps, window))

    @tool(annotations=READ_ONLY)
    async def agents_overview(window: Window = "7d") -> CallToolResult:
        """Sessions, model/tool calls, failures, tokens and cost for Claude Code and Codex."""
        return await run_tool(overview_agents(deps, window))


async def find_loop_suspects(deps: Deps, window: str = "24h", min_repeats: int = 3) -> ToolOutput:
    min_repeats = clamp(min_repeats, 2, 50)
    rng = window_range(window, deps.clock())
    cols = await load_columns(deps, "traces")
    rows = await deps.query(_loop_sql(deps, cols), "traces", rng, RAW_CAP)
    bursts, note = await _api_error_bursts(deps, rng, min_repeats)
    if not rows and not bursts:
        return empty("Loop suspects", f"No tool or LLM spans in the last {window}.")
    ranked = rank_suspects(merge_retry_storms(find_loops(rows, min_repeats) + bursts))
    return _render_loops(window, min_repeats, ranked, len(rows) >= RAW_CAP, note, cols)


async def compare_two_sessions(deps: Deps, a: str, b: str, window: str = "30d") -> ToolOutput:
    require_id("session_a", a)
    require_id("session_b", b)
    if a == b:
        raise ValueError("session_a and session_b must differ")
    rng = window_range(window, deps.clock())
    ids = in_list((a, b))
    logs = await deps.query(_session_logs_sql(deps, ids), "logs", rng, 10)
    models = await deps.query(_session_models_sql(deps, ids), "logs", rng, 100)
    spans, note = await deps.query_optional(_session_spans_sql(deps, ids), "traces", rng, 10)
    metrics = {sid: session_metrics(sid, logs, models, spans) for sid in (a, b)}
    if not any(m["found"] for m in metrics.values()):
        return empty("Session comparison", f"Neither session has events in the last {window}.")
    return _render_compare(window, metrics[a], metrics[b], note)


async def session_audit_trail(
    deps: Deps, session_id: str, include_commands: bool = False, window: str = "30d"
) -> ToolOutput:
    require_id("session_id", session_id)
    rng = window_range(window, deps.clock())
    with_cmd = include_commands and deps.settings.allow_content
    log_cols = await load_columns(deps, "logs")
    span_cols = await load_columns(deps, "traces")
    logs = await deps.query(_audit_logs_sql(deps, session_id, log_cols), "logs", rng, AUDIT_CAP)
    spans, note = await deps.query_optional(
        _audit_spans_sql(deps, session_id, span_cols, with_cmd), "traces", rng, AUDIT_CAP
    )
    entries = audit_entries(logs, spans, with_cmd)
    if not entries:
        return empty(f"Audit trail for {session_id}", f"No events in the last {window}.")
    meta = {
        "asked": include_commands,
        "with_cmd": with_cmd,
        "capped": len(logs) >= AUDIT_CAP or len(spans) >= AUDIT_CAP,
        "note": note,
    }
    return _render_audit(deps, session_id, window, entries, meta)


async def review_risky_actions(deps: Deps, window: str = "7d") -> ToolOutput:
    rng = window_range(window, deps.clock())
    log_cols = await load_columns(deps, "logs")
    span_cols = await load_columns(deps, "traces")
    findings: Findings = {}
    notes: list[str] = []
    collectors: tuple[Collector, ...] = (_risky_bash, _risky_decisions, _risky_modes, _risky_mcp)
    for collect in collectors:
        note = await collect(deps, rng, log_cols, span_cols, findings)
        if note:
            notes.append(note)
    ranked = rank_findings(findings.values())
    if not ranked and not notes:
        return empty("Risky actions", f"Nothing risky recorded in the last {window}.")
    return _render_risky(window, ranked, notes)


async def overview_agents(deps: Deps, window: str = "7d") -> ToolOutput:
    rng = window_range(window, deps.clock())
    claude = await _claude_summary(deps, rng)
    codex, note = await _codex_summary(deps, rng)
    return _render_agents(deps, window, claude, codex, note)


async def load_columns(deps: Deps, stream_type: StreamType, stream: str | None = None) -> Columns:
    """Read the stream schema; when it cannot be read, fall back to the verified column set."""
    name = stream or deps.settings.claude_stream
    try:
        payload = await deps.client.request(
            "GET", f"streams/{name}/schema", params={"type": stream_type}
        )
    except (O2Error, ValueError):
        payload = None
    schema = payload.get("schema") if isinstance(payload, dict) else None
    fields = None
    if isinstance(schema, list) and schema:
        fields = frozenset(str(f.get("name")) for f in schema if isinstance(f, dict))
    return Columns(stream_type, fields, deps.settings.allow_content)


def select_list(columns: Iterable[str]) -> str:
    return ", ".join(columns)


def find_loops(rows: Sequence[dict[str, Any]], min_repeats: int) -> list[Suspect]:
    """Pure loop detection over tool, tool.execution and llm_request spans."""
    out: list[Suspect] = []
    for trace_id, seq in tool_calls_by_trace(rows).items():
        out += repeated_runs(trace_id, seq, min_repeats)
        out += failure_chains(trace_id, seq)
        out += same_targets(trace_id, seq, min_repeats)
    return out + llm_retry_storms(rows, min_repeats)


def tool_calls_by_trace(rows: Sequence[dict[str, Any]]) -> dict[str, list[ToolCall]]:
    failed_parents = {
        str(r.get("reference_parent_span_id"))
        for r in rows
        if r.get("operation_name") == tr.OP_EXEC and _is_false(r.get("success"))
    }
    out: dict[str, list[ToolCall]] = {}
    for row in rows:
        if row.get("operation_name") != tr.OP_TOOL or not row.get("trace_id"):
            continue
        call = _tool_call(row)
        call.failed = call.span_id in failed_parents
        out.setdefault(str(row["trace_id"]), []).append(call)
    for seq in out.values():
        seq.sort(key=lambda c: c.start_ns)
    return out


def span_seconds(row: dict[str, Any]) -> float:
    duration_us = to_float(row.get("duration"))
    if duration_us is not None:
        return duration_us / 1e6
    return max(0.0, num(row.get("end_time")) - num(row.get("start_time"))) / 1e9


def repeated_runs(trace_id: str, seq: list[ToolCall], min_repeats: int) -> list[Suspect]:
    out: list[Suspect] = []
    start = 0
    for i in range(1, len(seq) + 1):
        if i < len(seq) and seq[i].key == seq[start].key:
            continue
        if i - start >= min_repeats:
            out.append(_run_suspect(trace_id, seq[start:i]))
        start = i
    return out


def failure_chains(trace_id: str, seq: list[ToolCall]) -> list[Suspect]:
    """Longest run of failures per call key; other tools in between do not break it."""
    best: dict[str, list[ToolCall]] = {}
    streak: dict[str, list[ToolCall]] = {}
    for call in seq:
        if not call.failed:
            streak[call.key] = []
            continue
        cur = streak.setdefault(call.key, [])
        cur.append(call)
        if len(cur) > len(best.get(call.key, [])):
            best[call.key] = list(cur)
    return [
        Suspect(
            trace_id=trace_id,
            session_id=chain[0].session_id,
            pattern="failure_retry_chain",
            detail=key,
            count=len(chain),
            wasted_s=sum(c.seconds for c in chain),
            failures=len(chain),
            confidence="high",
        )
        for key, chain in best.items()
        if len(chain) >= FAIL_CHAIN_MIN
    ]


def same_targets(trace_id: str, seq: list[ToolCall], min_repeats: int) -> list[Suspect]:
    groups: dict[tuple[str, str], list[ToolCall]] = {}
    for call in seq:
        if call.target and call.tool in SAME_TARGET_TOOLS:
            groups.setdefault((call.tool, call.target), []).append(call)
    return [
        Suspect(
            trace_id=trace_id,
            session_id=calls[0].session_id,
            pattern="same_target",
            detail=f"{tool} {target}",
            count=len(calls),
            wasted_s=sum(c.seconds for c in calls[1:]),
        )
        for (tool, target), calls in groups.items()
        if len(calls) >= min_repeats
    ]


def llm_retry_storms(rows: Sequence[dict[str, Any]], min_repeats: int) -> list[Suspect]:
    failed: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("operation_name") == tr.OP_LLM and _is_false(row.get("success")):
            failed.setdefault(str(row.get("trace_id") or ""), []).append(row)
    return [
        Suspect(
            trace_id=trace_id,
            session_id=str(spans[0].get("session_id") or ""),
            pattern="retry_storm",
            detail="failed llm_request spans",
            count=len(spans),
            wasted_s=sum(span_seconds(s) for s in spans),
            failures=len(spans),
            confidence="high",
        )
        for trace_id, spans in failed.items()
        if trace_id and len(spans) >= min_repeats
    ]


def merge_retry_storms(suspects: list[Suspect]) -> list[Suspect]:
    """A trace seen as a storm in both spans and logs is reported once."""
    merged: dict[str, Suspect] = {}
    out: list[Suspect] = []
    for s in suspects:
        if s.pattern != "retry_storm":
            out.append(s)
            continue
        prev = merged.setdefault(s.trace_id, s)
        if prev is s:
            continue
        prev.count = max(prev.count, s.count)
        prev.failures = max(prev.failures, s.failures)
        prev.wasted_s = max(prev.wasted_s, s.wasted_s)
        prev.session_id = prev.session_id or s.session_id
        prev.detail = "failed llm_request spans + api_error logs"
    return out + list(merged.values())


def rank_suspects(suspects: list[Suspect]) -> list[Suspect]:
    return sorted(
        suspects,
        key=lambda s: (CONFIDENCE_RANK[s.confidence], s.wasted_s, s.count),
        reverse=True,
    )


def session_metrics(
    sid: str, logs: list[dict], models: list[dict], spans: list[dict]
) -> dict[str, Any]:
    log = next((r for r in logs if str(r.get("session_id")) == sid), {})
    span = next((r for r in spans if str(r.get("session_id")) == sid), {})
    read, inp, write = (
        num(log.get(k)) for k in ("cache_read_tokens", "input_tokens", "cache_creation_tokens")
    )
    denom = read + inp + write
    p95 = to_float(span.get("p95_tool_us"))
    return {
        "session_id": sid,
        "found": bool(log or span),
        "cost_usd": round(num(log.get("cost_usd")), 4),
        "llm_calls": int(num(log.get("llm_calls"))),
        "tool_calls": int(num(span.get("tool_calls")) or num(log.get("tool_results"))),
        "tool_failures": int(num(log.get("tool_failures"))),
        "api_errors": int(num(log.get("api_errors"))),
        "permission_wait_min": round(num(span.get("wait_us")) / 60e6, 1),
        "cache_hit_pct": round(100 * read / denom, 1) if denom else None,
        "p95_tool_ms": round(p95 / 1000, 1) if p95 is not None else None,
        "models": [
            str(m["model"]) for m in models if str(m.get("session_id")) == sid and m.get("model")
        ],
    }


def ratio_flag(x: float | None, y: float | None) -> float | None:
    """max/min when one side is ≥2× the other (inf when only one side is non-zero)."""
    if x is None or y is None:
        return None
    hi, lo = max(x, y), min(x, y)
    if hi <= 0:
        return None
    if lo <= 0:
        return float("inf")
    ratio = hi / lo
    return round(ratio, 2) if ratio >= FLAG_RATIO else None


def audit_entries(
    logs: list[dict[str, Any]], spans: list[dict[str, Any]], with_cmd: bool
) -> list[dict[str, Any]]:
    """Merge log events and tool spans into one time-ordered list of content-free entries."""
    tool_rows = span_tool_entries(spans, with_cmd)
    entries = [e for e in (log_entry(r) for r in logs) if e is not None]
    if tool_rows:
        entries = [e for e in entries if e["event"] != "tool result"]
    entries += tool_rows
    entries.sort(key=lambda e: e["time_us"])
    return entries


def log_entry(row: dict[str, Any]) -> dict[str, Any] | None:
    event = str(row.get("event_name") or "")
    base = {
        "time_us": int(num(row.get("_timestamp"))),
        "event": event.replace("_", " "),
        "tool": row.get("tool_name"),
        "command_class": None,
        "decision": row.get("decision"),
        "source": row.get("source"),
        "outcome": None,
        "trace_id": row.get("trace_id"),
    }
    if event not in AUDIT_EVENTS:
        return None
    return {**base, **_log_entry_details(event, row)}


def span_tool_entries(spans: list[dict[str, Any]], with_cmd: bool) -> list[dict[str, Any]]:
    children: dict[str, dict[str, dict[str, Any]]] = {}
    for row in spans:
        op = str(row.get("operation_name") or "")
        if op in (tr.OP_BLOCKED, tr.OP_EXEC):
            parent = str(row.get("reference_parent_span_id") or "")
            children.setdefault(parent, {})[op] = row
    out = []
    for row in spans:
        if row.get("operation_name") != tr.OP_TOOL:
            continue
        kids = children.get(str(row.get("span_id") or ""), {})
        out.append(_tool_entry(row, kids.get(tr.OP_BLOCKED), kids.get(tr.OP_EXEC), with_cmd))
    return out


def classify_command(klass: str | None, argv0: str | None) -> tuple[str, int] | None:
    """Risk category of a Bash call from its class/argv0 only, never its text."""
    hits = [RISK_ARGV0.get((argv0 or "").lower()), RISK_CLASSES.get((klass or "").lower())]
    found = [h for h in hits if h]
    return max(found, key=lambda h: h[1]) if found else None


def rank_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.weight, f.count), reverse=True)


def agent_row(agent: str, row: dict[str, Any]) -> dict[str, Any]:
    cost = to_float(row.get("cost_usd"))
    sessions = to_float(row.get("sessions"))
    return {
        "agent": agent,
        "sessions": int(sessions) if sessions is not None else None,
        "model_calls": int(num(row.get("model_calls"))),
        "tool_calls": int(num(row.get("tool_calls"))),
        "failures": int(num(row.get("tool_failures")) + num(row.get("api_errors"))),
        "input_tokens": int(num(row.get("input_tokens"))),
        "output_tokens": int(num(row.get("output_tokens"))),
        "cached_tokens": int(num(row.get("cached_tokens"))),
        "cost_usd": round(cost, 4) if cost is not None else None,
    }


def codex_hint(deps: Deps) -> str:
    s = deps.settings
    return (
        "To add Codex CLI, export its OTel logs to OpenObserve in `~/.codex/config.toml`:\n\n"
        "```toml\n[otel]\nlog_user_prompt = false\n"
        "exporter = { otlp-http = { "
        f'endpoint = "{s.safe_url}/api/{s.org}/v1/logs", protocol = "binary", '
        'headers = { "Authorization" = "Basic <base64 user:password>", '
        f'"stream-name" = "{s.codex_stream}" }} }} }}\n```'
    )


def _is_false(value: Any) -> bool:
    return str(value).lower() == "false"


def _loop_sql(deps: Deps, cols: Columns) -> str:
    ops = in_list((tr.OP_TOOL, tr.OP_EXEC, tr.OP_LLM))
    picked = cols.pick(
        "session_id", "tool_name", "bash_argv0", "bash_command_class", "success", "file_path"
    )
    base = ["trace_id", "span_id", "reference_parent_span_id", "operation_name"]
    return (
        f"SELECT {select_list([*base, *picked, 'start_time', 'end_time', 'duration'])} "
        f"FROM {deps.claude} WHERE operation_name IN ({ops}) "
        f"ORDER BY _timestamp DESC LIMIT {RAW_CAP}"
    )


async def _api_error_bursts(
    deps: Deps, rng: TimeRange, min_repeats: int
) -> tuple[list[Suspect], str | None]:
    sql = (
        "SELECT trace_id, session_id, COUNT(*) AS n "
        f"FROM {deps.claude} WHERE event_name IN ({in_list(API_ERROR_EVENTS)}) "
        "AND trace_id IS NOT NULL "
        f"GROUP BY trace_id, session_id HAVING COUNT(*) >= {min_repeats} "
        "ORDER BY n DESC LIMIT 200"
    )
    rows, note = await deps.query_optional(sql, "logs", rng, 200)
    out = [
        Suspect(
            trace_id=str(r["trace_id"]),
            session_id=str(r.get("session_id") or ""),
            pattern="retry_storm",
            detail="api_error burst (logs)",
            count=int(num(r.get("n"))),
            wasted_s=0.0,
            failures=int(num(r.get("n"))),
            confidence="high",
        )
        for r in rows
        if r.get("trace_id")
    ]
    return out, (f"API error logs unavailable: {note}" if note else None)


def _tool_call(row: dict[str, Any]) -> ToolCall:
    tool = str(row.get("tool_name") or "(unknown)")
    detail = str(row.get("bash_argv0") or row.get("bash_command_class") or "")
    return ToolCall(
        span_id=str(row.get("span_id") or ""),
        key=f"{tool}:{detail}" if detail else tool,
        tool=tool,
        start_ns=int(num(row.get("start_time"))),
        seconds=span_seconds(row),
        session_id=str(row.get("session_id") or ""),
        target=str(row.get("file_path") or ""),
    )


def _run_suspect(trace_id: str, run: list[ToolCall]) -> Suspect:
    failures = sum(c.failed for c in run)
    exploratory = run[0].tool in EXPLORATORY_TOOLS
    confidence = "high" if failures else "low" if exploratory else "medium"
    return Suspect(
        trace_id=trace_id,
        session_id=run[0].session_id,
        pattern="repeated_call",
        detail=run[0].key,
        count=len(run),
        wasted_s=sum(c.seconds for c in run[1:]),
        failures=failures,
        confidence=confidence,
    )


def _render_loops(
    window: str,
    min_repeats: int,
    ranked: list[Suspect],
    sampled: bool,
    note: str | None,
    cols: Columns,
) -> ToolOutput:
    parts = [
        heading(
            f"Loop suspects, last {window}",
            f"min_repeats={min_repeats}; wasted = seconds spent in repeated or failed calls",
        )
    ]
    if ranked:
        rows = [
            [
                i,
                s.confidence,
                s.pattern,
                s.detail,
                s.count,
                s.failures,
                round(s.wasted_s, 1),
                s.trace_id,
            ]
            for i, s in enumerate(ranked[:25], 1)
        ]
        headers = ["#", "confidence", "pattern", "detail", "count", "fails", "wasted s", "trace_id"]
        wasted = round(sum(s.wasted_s for s in ranked), 1)
        parts += [table(headers, rows), f"**{len(ranked)} suspects, ~{wasted} s wasted.**"]
    else:
        parts.append("No stuck-agent patterns found.")
    if not cols.has("file_path"):
        parts.append(
            "_Same-file repeats need file_path, which is content (AGENT_OBS_ALLOW_CONTENT)._"
        )
    if sampled:
        parts.append(f"_Only the newest {RAW_CAP} spans were analysed; narrow the window._")
    if note:
        parts.append(f"_{note}_")
    data = {
        "window": window,
        "min_repeats": min_repeats,
        "suspects": [s.as_dict() for s in ranked],
        "sampled": sampled,
    }
    return ToolOutput("\n\n".join(parts), data)


def _session_logs_sql(deps: Deps, ids: str) -> str:
    api = "event_name = 'api_request'"
    return (
        "SELECT session_id, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(cost_usd AS DOUBLE) ELSE 0 END) AS cost_usd, "
        f"SUM(CASE WHEN {api} THEN 1 ELSE 0 END) AS llm_calls, "
        "SUM(CASE WHEN event_name = 'tool_result' THEN 1 ELSE 0 END) AS tool_results, "
        f"SUM(CASE WHEN event_name = 'tool_result' AND {FAILED} THEN 1 ELSE 0 END)"
        " AS tool_failures, "
        f"SUM(CASE WHEN event_name IN ({in_list(API_ERROR_EVENTS)}) THEN 1 ELSE 0 END)"
        " AS api_errors, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(input_tokens AS DOUBLE) ELSE 0 END) AS input_tokens, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(cache_read_tokens AS DOUBLE) ELSE 0 END)"
        " AS cache_read_tokens, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(cache_creation_tokens AS DOUBLE) ELSE 0 END)"
        " AS cache_creation_tokens "
        f"FROM {deps.claude} WHERE session_id IN ({ids}) GROUP BY session_id"
    )


def _session_models_sql(deps: Deps, ids: str) -> str:
    return (
        "SELECT session_id, model, COUNT(*) AS n "
        f"FROM {deps.claude} WHERE event_name = 'api_request' AND session_id IN ({ids}) "
        "GROUP BY session_id, model ORDER BY n DESC LIMIT 100"
    )


def _session_spans_sql(deps: Deps, ids: str) -> str:
    dur = "TRY_CAST(duration AS DOUBLE)"
    tool = f"operation_name = {sql_str(tr.OP_TOOL)}"
    blocked = f"operation_name = {sql_str(tr.OP_BLOCKED)}"
    return (
        "SELECT session_id, "
        f"SUM(CASE WHEN {tool} THEN 1 ELSE 0 END) AS tool_calls, "
        f"SUM(CASE WHEN {blocked} THEN {dur} ELSE 0 END) AS wait_us, "
        f"approx_percentile_cont(CASE WHEN {tool} THEN {dur} END, 0.95) AS p95_tool_us "
        f"FROM {deps.claude} WHERE session_id IN ({ids}) "
        f"AND operation_name IN ({in_list((tr.OP_TOOL, tr.OP_BLOCKED))}) GROUP BY session_id"
    )


def _render_compare(
    window: str, a: dict[str, Any], b: dict[str, Any], note: str | None
) -> ToolOutput:
    rows, flags = [], []
    for key, label in COMPARE_METRICS:
        ratio = ratio_flag(a[key], b[key])
        rows.append([label, a[key], b[key], _flag_text(ratio)])
        if ratio is not None:
            higher = a["session_id"] if (a[key] or 0) > (b[key] or 0) else b["session_id"]
            finite = None if ratio == float("inf") else ratio
            flags.append({"metric": key, "ratio": finite, "higher": higher})
    rows.append(["models", ", ".join(a["models"]) or None, ", ".join(b["models"]) or None, ""])
    parts = [
        heading(f"Session comparison, last {window}", "⚠ = one side is ≥2× the other"),
        table(["metric", a["session_id"], b["session_id"], "flag"], rows),
    ]
    parts += [
        f"_Session {side['session_id']} has no events in the window._"
        for side in (a, b)
        if not side["found"]
    ]
    if note:
        parts.append(f"_Span metrics unavailable: {note}_")
    return ToolOutput("\n\n".join(parts), {"window": window, "a": a, "b": b, "flags": flags})


def _flag_text(ratio: float | None) -> str:
    if ratio is None:
        return ""
    return "⚠ only one side" if ratio == float("inf") else f"⚠ {ratio}×"


def _audit_logs_sql(deps: Deps, session_id: str, cols: Columns) -> str:
    picked = cols.pick(
        "trace_id",
        "tool_name",
        "decision",
        "source",
        "success",
        "status_code",
        "model",
        "server_name",
        "status",
        "from_mode",
        "to_mode",
        "mode",
        "trigger",
    )
    return (
        f"SELECT {select_list(['_timestamp', 'event_name', *picked])} FROM {deps.claude} "
        f"WHERE session_id = {sql_str(session_id)} AND event_name IN ({in_list(AUDIT_EVENTS)}) "
        f"ORDER BY _timestamp ASC LIMIT {AUDIT_CAP}"
    )


def _audit_spans_sql(deps: Deps, session_id: str, cols: Columns, with_cmd: bool) -> str:
    picked = cols.pick(
        "tool_name",
        "bash_command_class",
        "bash_argv0",
        "decision",
        "source",
        "success",
        "error_class",
    )
    if with_cmd:
        picked += cols.pick("full_command")
    base = ["start_time", "trace_id", "span_id", "reference_parent_span_id", "operation_name"]
    ops = in_list((tr.OP_TOOL, tr.OP_BLOCKED, tr.OP_EXEC))
    return (
        f"SELECT {select_list([*base, *picked])} FROM {deps.claude} "
        f"WHERE session_id = {sql_str(session_id)} AND operation_name IN ({ops}) "
        f"ORDER BY start_time ASC LIMIT {AUDIT_CAP}"
    )


def _log_entry_details(event: str, row: dict[str, Any]) -> dict[str, Any]:
    if event == "tool_decision":
        return {"event": "permission decision"}
    if event == "tool_result":
        return {"event": "tool result", "outcome": _ok(row.get("success"))}
    if event == "mcp_server_connection":
        return {
            "event": "mcp connection",
            "tool": row.get("server_name"),
            "outcome": row.get("status"),
        }
    if event == "permission_mode_changed":
        return {
            "event": "permission mode change",
            "decision": _mode_change(row),
            "source": row.get("trigger"),
        }
    if event == "user_prompt":
        return {"event": "user prompt (redacted)", "tool": None}
    status = row.get("status_code")
    return {"tool": row.get("model"), "outcome": f"HTTP {status}" if status else "error"}


def _ok(value: Any) -> str | None:
    return {"true": "ok", "false": "failed"}.get(str(value).lower())


def _mode_change(row: dict[str, Any]) -> str | None:
    to_mode = row.get("to_mode") or row.get("mode")
    if row.get("from_mode") and to_mode:
        return f"{row['from_mode']} → {to_mode}"
    return str(to_mode) if to_mode else None


def _tool_entry(
    row: dict[str, Any], blocked: dict | None, execution: dict | None, with_cmd: bool
) -> dict[str, Any]:
    klass, argv0 = row.get("bash_command_class"), row.get("bash_argv0")
    entry = {
        "time_us": int(num(row.get("start_time")) / 1000),
        "event": "tool call",
        "tool": row.get("tool_name"),
        "command_class": "/".join(str(v) for v in (klass, argv0) if v) or None,
        "decision": blocked.get("decision") if blocked else "no prompt",
        "source": blocked.get("source") if blocked else None,
        "outcome": _exec_outcome(execution, blocked),
        "trace_id": row.get("trace_id"),
    }
    if with_cmd:
        entry["command"] = row.get("full_command")
    return entry


def _exec_outcome(execution: dict | None, blocked: dict | None) -> str:
    if execution is None:
        return "not run" if blocked and blocked.get("decision") == "reject" else "unknown"
    if _is_false(execution.get("success")):
        return f"failed ({execution.get('error_class') or 'error'})"
    return "ok"


def _render_audit(
    deps: Deps, session_id: str, window: str, entries: list[dict[str, Any]], meta: dict[str, Any]
) -> ToolOutput:
    tz = deps.settings.tz
    counts = Counter(e["event"] for e in entries)
    first, last = fmt_us_ts(entries[0]["time_us"], tz), fmt_us_ts(entries[-1]["time_us"], tz)
    header = [
        heading(f"Audit trail — session {session_id}", f"times in {deps.settings.tz_name}"),
        f"- Window: last {window}; events {first} → {last}",
        f"- Entries: {len(entries)} ("
        + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        + ")",
        f"- {_redaction_line(meta['asked'], meta['with_cmd'])}",
    ]
    if meta["capped"]:
        header.append(f"- ⚠ Capped at {AUDIT_CAP} rows per source; narrow the window.")
    if meta["note"]:
        header.append(f"- Span data unavailable: {meta['note']}")
    with_cmd = meta["with_cmd"]
    cols = ["time", "event", "tool", "command class", "decision", "source", "outcome", "trace_id"]
    body = table(
        [*cols, "command"] if with_cmd else cols, [_audit_row(e, tz, with_cmd) for e in entries]
    )
    data = {
        "session_id": session_id,
        "window": window,
        "content_redacted": not with_cmd,
        "counts": dict(counts),
        "entries": [{**e, "time": fmt_us_ts(e["time_us"], tz)} for e in entries],
    }
    return ToolOutput("\n".join(header) + "\n\n" + body, data)


def _redaction_line(asked: bool, with_cmd: bool) -> str:
    if with_cmd:
        return "Commands INCLUDED (include_commands=True and AGENT_OBS_ALLOW_CONTENT=1)."
    line = "Content REDACTED: no prompt, tool input, command, file path or output was queried."
    if asked:
        line += " include_commands was ignored because AGENT_OBS_ALLOW_CONTENT is off."
    return line


def _audit_row(entry: dict[str, Any], tz: Any, with_cmd: bool) -> list[Any]:
    row = [
        fmt_us_ts(entry["time_us"], tz),
        entry["event"],
        entry["tool"],
        entry["command_class"],
        entry["decision"],
        entry["source"],
        entry["outcome"],
        entry["trace_id"],
    ]
    return [*row, entry.get("command")] if with_cmd else row


def _finding(findings: Findings, category: str, item: str, weight: int) -> Finding:
    return findings.setdefault((category, item), Finding(category, item, weight))


async def _risky_bash(
    deps: Deps, rng: TimeRange, _logs: Columns, spans: Columns, findings: Findings
) -> str | None:
    picked = spans.pick("bash_command_class", "bash_argv0")
    if not picked:
        return "Bash command class/argv0 are not in the trace schema; command risk skipped."
    sql = (
        f"SELECT {select_list(picked)}, COUNT(*) AS n, MAX(trace_id) AS example "
        f"FROM {deps.claude} WHERE operation_name = {sql_str(tr.OP_TOOL)} "
        f"AND tool_name = 'Bash' GROUP BY {select_list(picked)} ORDER BY n DESC LIMIT 500"
    )
    rows, note = await deps.query_optional(sql, "traces", rng, 500)
    for row in rows:
        hit = classify_command(row.get("bash_command_class"), row.get("bash_argv0"))
        if hit:
            label = str(row.get("bash_argv0") or row.get("bash_command_class"))
            _finding(findings, f"bash: {hit[0]}", label, hit[1]).add(
                int(num(row.get("n"))), row.get("example")
            )
    return f"Bash spans unavailable: {note}" if note else None


async def _risky_decisions(
    deps: Deps, rng: TimeRange, _logs: Columns, _spans: Columns, findings: Findings
) -> str | None:
    sql = (
        "SELECT tool_name, decision, source, COUNT(*) AS n, MAX(trace_id) AS example "
        f"FROM {deps.claude} WHERE event_name = 'tool_decision' "
        "AND (decision = 'reject' OR source IN ('user_abort', 'user_reject')) "
        "GROUP BY tool_name, decision, source ORDER BY n DESC LIMIT 200"
    )
    rows, note = await deps.query_optional(sql, "logs", rng, 200)
    for row in rows:
        source = str(row.get("source") or "unknown")
        category = "user abort" if source == "user_abort" else f"permission {row.get('decision')}"
        weight = 3 if source in ("user_abort", "user_reject") else 2
        _finding(findings, category, f"{row.get('tool_name')} ({source})", weight).add(
            int(num(row.get("n"))), row.get("example")
        )
    return f"tool_decision events unavailable: {note}" if note else None


async def _risky_modes(
    deps: Deps, rng: TimeRange, logs: Columns, _spans: Columns, findings: Findings
) -> str | None:
    picked = logs.pick("to_mode", "mode", "from_mode", "trigger")
    if not picked:
        return None
    keys = select_list(["session_id", *picked])
    sql = (
        f"SELECT {keys}, COUNT(*) AS n FROM {deps.claude} "
        f"WHERE event_name = 'permission_mode_changed' GROUP BY {keys} ORDER BY n DESC LIMIT 200"
    )
    rows, note = await deps.query_optional(sql, "logs", rng, 200)
    for row in rows:
        to_mode = str(row.get("to_mode") or row.get("mode") or "unknown")
        trigger = f" via {row['trigger']}" if row.get("trigger") else ""
        _finding(
            findings, "permission mode change", f"→ {to_mode}{trigger}", MODE_RISK.get(to_mode, 1)
        ).add(int(num(row.get("n"))), f"session {row.get('session_id')}")
    return f"permission_mode_changed unavailable: {note}" if note else None


async def _risky_mcp(
    deps: Deps, rng: TimeRange, logs: Columns, _spans: Columns, findings: Findings
) -> str | None:
    picked = logs.pick("server_name", "status")
    if "status" not in picked:
        return None
    keys = select_list(picked)
    sql = (
        f"SELECT {keys}, COUNT(*) AS n, MAX(session_id) AS example FROM {deps.claude} "
        f"WHERE event_name = 'mcp_server_connection' GROUP BY {keys} ORDER BY n DESC LIMIT 200"
    )
    rows, note = await deps.query_optional(sql, "logs", rng, 200)
    for row in rows:
        status = str(row.get("status") or "unknown")
        server = str(row.get("server_name") or "(name not exported)")
        failed = status == "failed"
        _finding(
            findings, "mcp failure" if failed else f"mcp {status}", server, 2 if failed else 1
        ).add(int(num(row.get("n"))), f"session {row.get('example')}")
    return f"mcp_server_connection unavailable: {note}" if note else None


def _render_risky(window: str, ranked: list[Finding], notes: list[str]) -> ToolOutput:
    parts = [
        heading(
            f"Risky actions, last {window}",
            "ranked by risk; Bash matched on command class/argv0 only, never command text",
        )
    ]
    if ranked:
        rows = [
            [f.severity, f.category, f.item, f.count, ", ".join(f.examples)] for f in ranked[:40]
        ]
        parts.append(table(["severity", "category", "item", "count", "examples"], rows))
    else:
        parts.append("Nothing risky recorded.")
    parts += [f"_{n}_" for n in notes]
    data = {"window": window, "findings": [f.as_dict() for f in ranked], "notes": notes}
    return ToolOutput("\n\n".join(parts), data)


async def _claude_summary(deps: Deps, rng: TimeRange) -> dict[str, Any]:
    api = "event_name = 'api_request'"
    sql = (
        "SELECT COUNT(DISTINCT session_id) AS sessions, "
        f"SUM(CASE WHEN {api} THEN 1 ELSE 0 END) AS model_calls, "
        "SUM(CASE WHEN event_name = 'tool_result' THEN 1 ELSE 0 END) AS tool_calls, "
        f"SUM(CASE WHEN event_name = 'tool_result' AND {FAILED} THEN 1 ELSE 0 END)"
        " AS tool_failures, "
        f"SUM(CASE WHEN event_name IN ({in_list(API_ERROR_EVENTS)}) THEN 1 ELSE 0 END)"
        " AS api_errors, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(input_tokens AS DOUBLE) ELSE 0 END) AS input_tokens, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(output_tokens AS DOUBLE) ELSE 0 END)"
        " AS output_tokens, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(cache_read_tokens AS DOUBLE) ELSE 0 END)"
        " AS cached_tokens, "
        f"SUM(CASE WHEN {api} THEN TRY_CAST(cost_usd AS DOUBLE) ELSE 0 END) AS cost_usd "
        f"FROM {deps.claude}"
    )
    rows = await deps.query(sql, "logs", rng, 1)
    return agent_row("Claude Code", rows[0] if rows else {})


async def _codex_summary(deps: Deps, rng: TimeRange) -> tuple[dict[str, Any] | None, str | None]:
    stream = deps.settings.codex_stream
    cols = await load_columns(deps, "logs", stream)
    rows, note = await deps.query_optional(_codex_sql(deps, cols), "logs", rng, 1)
    if note and cols.fields is None:
        bare = Columns("logs", frozenset(), False)
        rows, note = await deps.query_optional(_codex_sql(deps, bare), "logs", rng, 1)
    row = rows[0] if rows else {}
    if note or not int(num(row.get("events"))):
        return None, note or f"stream {stream!r} has no Codex events in the window"
    return agent_row("Codex CLI", row), None


def _codex_sql(deps: Deps, cols: Columns) -> str:
    def ev(kind: str) -> str:
        return f"event_name IN ({in_list(CODEX_EVENTS[kind])})"

    def total(name: str, alias: str, kind: str) -> str:
        if not cols.has(name):
            return f"NULL AS {alias}"
        return f"SUM(CASE WHEN {ev(kind)} THEN TRY_CAST({name} AS DOUBLE) ELSE 0 END) AS {alias}"

    failed = FAILED if cols.has("success") else "FALSE"
    model = f"({ev('api')} OR {ev('ws')})"
    sessions = "COUNT(DISTINCT conversation_id)" if cols.has("conversation_id") else "NULL"
    parts = [
        "COUNT(*) AS events",
        f"{sessions} AS sessions",
        f"SUM(CASE WHEN {model} THEN 1 ELSE 0 END) AS model_calls",
        f"SUM(CASE WHEN {ev('tool')} THEN 1 ELSE 0 END) AS tool_calls",
        f"SUM(CASE WHEN {ev('tool')} AND {failed} THEN 1 ELSE 0 END) AS tool_failures",
        f"SUM(CASE WHEN {model} AND {failed} THEN 1 ELSE 0 END) AS api_errors",
        total("input_token_count", "input_tokens", "sse"),
        total("output_token_count", "output_tokens", "sse"),
        total("cached_token_count", "cached_tokens", "sse"),
        total("usage_estimated_usd", "cost_usd", "cost"),
    ]
    return f"SELECT {', '.join(parts)} FROM {quote_ident(deps.settings.codex_stream)}"


def _render_agents(
    deps: Deps,
    window: str,
    claude: dict[str, Any],
    codex: dict[str, Any] | None,
    note: str | None,
) -> ToolOutput:
    agents = [claude] + ([codex] if codex else [])
    headers = [
        "agent",
        "sessions",
        "model calls",
        "tool calls",
        "failures",
        "input tok",
        "output tok",
        "cached tok",
        "cost $",
    ]
    parts = [
        heading(f"Coding agents, last {window}"),
        table(headers, [list(a.values()) for a in agents]),
    ]
    if codex is None:
        parts += [f"_Codex: no data ({note})._", codex_hint(deps)]
    else:
        parts.append("_Codex cost is the codex.turn_cost estimate, when Codex exports it._")
    data: dict[str, Any] = {
        "window": window,
        "agents": agents,
        "codex_connected": codex is not None,
    }
    if codex is None:
        data["codex_note"] = note
    return ToolOutput("\n\n".join(parts), data)
