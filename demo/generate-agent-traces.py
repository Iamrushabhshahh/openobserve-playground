#!/usr/bin/env python3
"""Generate synthetic Claude Code agent telemetry: traces, matching logs and metrics.

Lets you explore OpenObserve tracing without running Claude Code. The data has the same
shape as real Claude Code telemetry (span names, attributes, event logs), so the Claude
Code dashboards and the log <-> trace links work on it.

    python3 demo/generate-agent-traces.py                 # 6 hours of history, ~30 tasks
    python3 demo/generate-agent-traces.py --live          # then keep emitting a task every ~20 s
    python3 demo/generate-agent-traces.py --hours 24 --tasks 80

What one task looks like (one trace):

    claude_code.interaction                         (service: claude-code)
    ├── claude_code.llm_request                     model call: ttft, tokens, cost, stop_reason
    ├── claude_code.tool  (Bash)
    │   ├── claude_code.tool.blocked_on_user        waiting for you to approve
    │   └── claude_code.tool.execution
    ├── claude_code.tool  (mcp__github__...)        span kind CLIENT
    │   └── mcp.tools/call                          (service: github-mcp, kind SERVER)
    │       └── GET api.github.com                  (service: github-mcp, kind CLIENT)
    ├── claude_code.tool  (Agent)                   subagent: nested llm_request + tools
    └── claude_code.llm_request                     final answer (stop_reason=end_turn)

Every record carries demo.synthetic=true. Only the Python standard library is used.
Reads ZO_ROOT_USER_EMAIL / ZO_ROOT_USER_PASSWORD from .env (or the environment).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

NS = 1_000_000_000
MS = 1_000_000

MODEL_MAIN = "claude-opus-5-5"
MODEL_FAST = "claude-haiku-4-5-20251001"
# USD per million tokens: input, output, cache read, cache write (illustrative list prices)
PRICES = {MODEL_MAIN: (5.0, 25.0, 0.5, 6.25), MODEL_FAST: (1.0, 5.0, 0.1, 1.25)}

REPOS = ["acme/payments-api", "acme/web-frontend", "acme/infra"]
DEVELOPERS = ["alice@example.com", "bob@example.com", "chen@example.com", "dana@example.com"]
MCP_SERVERS = [("github", "http"), ("linear", "sse"), ("postgres", "stdio")]
TASKS = [
    ("Fix the failing test in test_refunds.py", ["Read", "Grep", "Read", "Edit", "Bash:pytest-fail", "Edit", "Bash:pytest"]),
    ("Add a /healthz endpoint to the API", ["Glob", "Read", "Write", "Edit", "Bash:pytest"]),
    ("Why is the checkout page slow? Check the open GitHub issues", ["mcp:github:list_issues", "mcp:github:get_issue", "Read", "Grep", "WebFetch"]),
    ("Refactor the retry helper and update its callers", ["Grep", "Read", "Agent", "Edit", "Edit", "Bash:pytest"]),
    ("Bump the Terraform AWS provider and plan", ["Read", "Edit", "Bash:terraform"]),
    ("Summarise yesterday's deploy failures", ["mcp:github:list_workflow_runs", "Bash:gh", "Read"]),
    ("Write a migration for the new invoices table", ["Read", "Glob", "Write", "Bash:alembic", "Bash:pytest"]),
]
TOOL_MS = {  # (min, max) execution time in ms
    "Read": (8, 60), "Grep": (20, 400), "Glob": (10, 120), "Edit": (15, 90), "Write": (15, 80),
    "WebFetch": (600, 6000), "Bash:pytest": (2500, 28000), "Bash:pytest-fail": (2500, 20000),
    "Bash:terraform": (8000, 45000), "Bash:gh": (700, 3000), "Bash:alembic": (900, 4000),
}
METRIC_ATTRS = {"app.entrypoint": "cli", "app.version": "2.1.289"}
NEEDS_APPROVAL = {"Edit", "Write", "Bash", "WebFetch", "mcp"}


# ---------------------------------------------------------------- OTLP helpers

def attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        # OpenObserve's OTLP/JSON endpoints reject doubleValue ("invalid type: map, expected
        # f64"), so floats travel as strings. Protobuf exporters are not affected.
        return {"key": key, "value": {"stringValue": repr(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def attrs(d: dict) -> list:
    return [attr(k, v) for k, v in d.items() if v is not None]


@dataclass
class Span:
    service: str
    name: str
    trace_id: str
    start: int
    end: int
    parent: str | None = None
    kind: int = 1  # 1 internal, 2 server, 3 client
    attributes: dict = field(default_factory=dict)
    error: str | None = None
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))

    def otlp(self) -> dict:
        s = {
            "traceId": self.trace_id, "spanId": self.span_id, "name": self.name, "kind": self.kind,
            "startTimeUnixNano": str(self.start), "endTimeUnixNano": str(self.end),
            "attributes": attrs(self.attributes),
            "status": {"code": 2, "message": self.error} if self.error else {"code": 0},
        }
        if self.parent:
            s["parentSpanId"] = self.parent
        return s


@dataclass
class Batch:
    spans: list = field(default_factory=list)
    logs: list = field(default_factory=list)  # (time_ns, trace_id, span_id, event_name, attributes)
    cost: list = field(default_factory=list)  # (time_ns, usd, model, repo)
    tokens: list = field(default_factory=list)  # (time_ns, count, type, model, repo)


# ---------------------------------------------------------------- simulation

class Task:
    def __init__(self, rng: random.Random, start_ns: int, session_id: str, repo: str, seq: int,
                 user_email: str = "alice@example.com"):
        self.rng, self.t, self.session_id, self.repo, self.seq = rng, start_ns, session_id, repo, seq
        self.user_email = user_email
        self.trace_id = secrets.token_hex(16)
        self.prompt_id = str(uuid.uuid4())
        self.batch = Batch()
        self.event_seq = 0

    def ms(self, lo: float, hi: float) -> int:
        return int(self.rng.uniform(lo, hi) * MS)

    def log(self, when: int, span: Span, event: str, **fields) -> None:
        self.event_seq += 1
        base = {"event.name": event, "session.id": self.session_id, "prompt.id": self.prompt_id,
                "event.sequence": self.event_seq, "user.email": self.user_email, "demo.synthetic": True}
        self.batch.logs.append((when, span.trace_id, span.span_id, event, {**base, **fields}))

    def llm(self, parent: Span, model: str, final: bool, agent_id: str | None = None) -> None:
        start = self.t
        ttft = self.ms(600, 2600) if model == MODEL_MAIN else self.ms(250, 900)
        dur = ttft + self.ms(1500, 14000 if model == MODEL_MAIN else 3000)
        cache_read = self.rng.randint(25_000, 140_000)
        inp, out = self.rng.randint(4, 900), self.rng.randint(80, 2400)
        cache_write = self.rng.randint(0, 6000)
        p = PRICES[model]
        cost = (inp * p[0] + out * p[1] + cache_read * p[2] + cache_write * p[3]) / 1e6
        stop = "end_turn" if final else "tool_use"
        request_id = "req_" + secrets.token_hex(12)
        attempt = 1
        if self.rng.random() < 0.04:  # occasional overloaded API: one failed attempt, then success
            attempt = 2
            retry_wait = self.ms(800, 4000)
            dur += retry_wait
        span = Span("claude-code", "claude_code.llm_request", self.trace_id, start, start + dur, parent.span_id,
                    attributes={
                        "model": model, "gen_ai.system": "anthropic", "gen_ai.request.model": model,
                        "ttft_ms": str(ttft // MS), "duration_ms": str(dur // MS),
                        "input_tokens": str(inp), "output_tokens": str(out),
                        "cache_read_tokens": str(cache_read), "cache_creation_tokens": str(cache_write),
                        "stop_reason": stop, "request_id": request_id, "attempt": str(attempt), "success": "true",
                        "user_email": self.user_email,
                        "llm_request.context": "tool" if agent_id else "interaction",
                        "query_source_safe": "subagent" if agent_id else "repl_main_thread",
                        "agent_id": agent_id, "session.id": self.session_id, "demo.synthetic": True,
                        # OpenTelemetry GenAI semantic conventions: OpenObserve's LLM views read these.
                        "gen_ai.operation.name": "chat", "gen_ai.response.model": model,
                        "gen_ai.usage.input_tokens": inp + cache_read + cache_write,
                        "gen_ai.usage.output_tokens": out,
                        "gen_ai.usage.cache_read_input_tokens": cache_read,
                        "gen_ai.usage.cache_creation_input_tokens": cache_write,
                        "gen_ai.usage.cost": round(cost, 6),
                        "gen_ai.response.finish_reasons": stop,
                    })
        self.batch.spans.append(span)
        if attempt == 2:
            self.log(start + retry_wait, span, "api_error", model=model, status_code="529", error="Overloaded",
                     attempt=1, duration_ms=str(retry_wait // MS))
        self.log(span.end, span, "api_request", model=model, cost_usd=round(cost, 6), duration_ms=str(dur // MS),
                 ttft_ms=ttft // MS, input_tokens=inp, output_tokens=out, cache_read_tokens=cache_read,
                 cache_creation_tokens=cache_write, request_id=request_id,
                 query_source="subagent" if agent_id else "repl_main_thread")
        if not agent_id:
            self.log(span.end, span, "assistant_response", model=model, response_length=out * 4, request_id=request_id)
        when = span.end
        self.batch.cost.append((when, cost, model, self.repo))
        for kind, n in (("input", inp), ("output", out), ("cacheRead", cache_read), ("cacheCreation", cache_write)):
            self.batch.tokens.append((when, n, kind, model, self.repo))
        self.t = span.end + self.ms(5, 40)

    def tool(self, parent: Span, spec: str, agent_id: str | None = None) -> None:
        name, _, detail = spec.partition(":")
        start = self.t
        tool_use_id = "toolu_" + secrets.token_hex(12)
        is_mcp = name == "mcp"
        tool_name = f"mcp__{detail.replace(':', '__')}" if is_mcp else name
        tool = Span("claude-code", "claude_code.tool", self.trace_id, start, start, parent.span_id,
                    kind=3 if is_mcp else 1,
                    attributes={"tool_name": tool_name, "tool_name_safe": "mcp_other" if is_mcp else name,
                                "tool_use_id": tool_use_id, "agent_id": agent_id,
                                "session.id": self.session_id, "demo.synthetic": True})
        if name == "Bash":
            tool.attributes.update({"bash_argv0": detail.split("-")[0], "bash_command_class":
                                    {"pytest": "test_runner", "terraform": "infra", "gh": "vcs", "alembic": "database"}
                                    .get(detail.split("-")[0], "other")})
        t = start
        hook_ms = self.ms(15, 180)
        self.log(t, tool, "hook_execution_start", hook_event="PreToolUse", hook_name=f"PreToolUse:{name}")
        self.log(t + hook_ms, tool, "hook_execution_complete", hook_event="PreToolUse",
                 hook_name=f"PreToolUse:{name}", total_duration_ms=str(hook_ms // MS), num_hooks=1, num_success=1,
                 num_blocking=0)
        t += hook_ms
        # permission wait
        family = "mcp" if is_mcp else name
        if family in NEEDS_APPROVAL and self.rng.random() < 0.55:
            wait = self.ms(400, 22000)
            blocked = Span("claude-code", "claude_code.tool.blocked_on_user", self.trace_id, t, t + wait, tool.span_id,
                           attributes={"duration_ms": str(wait // MS), "decision": "accept", "source": "user_temporary",
                                       "demo.synthetic": True})
            self.batch.spans.append(blocked)
            self.log(blocked.end, tool, "tool_decision", tool_name=tool_name, decision="accept",
                     source="user_temporary", tool_use_id=tool_use_id)
            self.log(blocked.end, tool, "hook_execution_complete", hook_event="PermissionRequest",
                     hook_name=f"PermissionRequest:{name}", total_duration_ms=str(wait // MS), num_hooks=1,
                     num_success=1, num_blocking=0)
            t = blocked.end
        else:
            self.log(t, tool, "tool_decision", tool_name=tool_name, decision="accept", source="config",
                     tool_use_id=tool_use_id)

        lo, hi = TOOL_MS.get(spec, TOOL_MS.get(name, (300, 2500)))
        if name == "Agent":
            exec_start = t
            execution = Span("claude-code", "claude_code.tool.execution", self.trace_id, t, t, tool.span_id,
                             attributes={"tool_use_id": tool_use_id, "success": "true", "demo.synthetic": True})
            self.batch.spans.append(execution)
            self.t = t + self.ms(20, 80)
            sub = "agent_" + secrets.token_hex(4)
            self.llm(execution, MODEL_FAST, final=False, agent_id=sub)
            for s in ("Grep", "Read", "Read"):
                self.tool(execution, s, agent_id=sub)
                self.llm(execution, MODEL_FAST, final=s == "Read" and self.rng.random() < 0.5, agent_id=sub)
            execution.end = self.t
            execution.attributes["duration_ms"] = str((execution.end - exec_start) // MS)
            t = execution.end
            success, err = True, None
        else:
            dur = self.ms(lo, hi)
            failed = detail == "pytest-fail" or (self.rng.random() < 0.03)
            success, err = not failed, ("ShellError: exit code 1" if failed else None)
            execution = Span("claude-code", "claude_code.tool.execution", self.trace_id, t, t + dur, tool.span_id,
                             attributes={"tool_use_id": tool_use_id, "duration_ms": str(dur // MS),
                                         "success": str(success).lower(),
                                         "error_class": "shell_error" if failed else None,
                                         "error": "ShellError" if failed else None, "demo.synthetic": True},
                             error=err)
            self.batch.spans.append(execution)
            if is_mcp:
                srv_start = t + self.ms(5, 30)
                srv_end = t + dur - self.ms(2, 10)
                server = Span("github-mcp", "mcp.tools/call", self.trace_id, srv_start, srv_end, tool.span_id, kind=2,
                              attributes={"mcp.method": "tools/call", "mcp.tool": detail.split(":")[-1],
                                          "rpc.system": "jsonrpc", "demo.synthetic": True})
                api_start = srv_start + self.ms(3, 15)
                api = Span("github-mcp", "GET api.github.com", self.trace_id, api_start, srv_end - self.ms(1, 5),
                           server.span_id, kind=3,
                           attributes={"http.request.method": "GET", "server.address": "api.github.com",
                                       "http.response.status_code": 200, "demo.synthetic": True})
                self.batch.spans += [server, api]
            t = execution.end
        tool.end = t
        tool.attributes["duration_ms"] = str((tool.end - tool.start) // MS)
        tool.attributes["success"] = str(success).lower()
        if err:
            tool.error = err
        self.batch.spans.append(tool)
        self.log(t, tool, "tool_result", tool_name=tool_name, success=str(success).lower(),
                 duration_ms=str((execution.end - execution.start) // MS), tool_use_id=tool_use_id,
                 decision_type="accept", error_type="ShellError" if err else None,
                 tool_result_size_bytes=self.rng.randint(200, 40000))
        self.t = t + self.ms(5, 60)

    def run(self, prompt: str, steps: list[str]) -> Batch:
        root = Span("claude-code", "claude_code.interaction", self.trace_id, self.t, self.t,
                    attributes={"interaction.sequence": self.seq, "user_prompt_length": len(prompt),
                                "session.id": self.session_id, "parent.source": "none", "demo.synthetic": True})
        if self.seq == 1 or self.rng.random() < 0.25:  # session start: MCP servers connect
            for server, transport in MCP_SERVERS:
                failed = server == "postgres" and self.rng.random() < 0.35
                self.log(self.t, root, "mcp_server_connection", status="failed" if failed else "connected",
                         server_name=server, transport_type=transport, server_scope="user",
                         duration_ms=str(self.rng.randint(40, 2500)), error_code="ECONNREFUSED" if failed else None)
        if self.rng.random() < 0.3:
            to_mode = self.rng.choice(["acceptEdits", "plan", "default"])
            self.log(self.t, root, "permission_mode_changed", from_mode="default", to_mode=to_mode,
                     trigger="shift_tab")
        self.log(self.t, root, "user_prompt", prompt_length=len(prompt))
        self.t += self.ms(20, 120)
        for step in steps:
            self.llm(root, MODEL_MAIN, final=False)
            self.tool(root, step)
        self.llm(root, MODEL_MAIN, final=True)
        root.end = self.t
        root.attributes["interaction.duration_ms"] = str((root.end - root.start) // MS)
        self.batch.spans.append(root)
        return self.batch


# ---------------------------------------------------------------- export

def _resource_dict(service: str, repo: str | None, env: str) -> dict:
    r = {"service.name": service, "service.version": "2.1.289" if service == "claude-code" else "0.9.0",
         "deployment.environment": env, "host.arch": "arm64", "os.type": "darwin", "demo.synthetic": True}
    if repo and service == "claude-code":
        owner, name = repo.split("/")
        r.update({"vcs.repository.name": name, "vcs.owner.name": owner,
                  "vcs.repository.url.full": f"https://github.com/{repo}", "vcs.provider.name": "github"})
    return r


def resource(service: str, repo: str | None, env: str) -> dict:
    return {"attributes": attrs(_resource_dict(service, repo, env))}


class Exporter:
    def __init__(self, url: str, org: str, auth: str, stream: str, env: str):
        self.api = f"{url.rstrip('/')}/api/{org}"
        self.base = f"{self.api}/v1"
        self.stream = stream
        self.headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json",
                        "stream-name": stream}
        self.env = env

    def post(self, path: str, body, base: str | None = None) -> None:
        req = urllib.request.Request(f"{base or self.base}/{path}", data=json.dumps(body).encode(),
                                     headers=self.headers)
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            sys.exit(f"OpenObserve rejected {path}: HTTP {e.code} {e.read()[:300]!r}")
        except urllib.error.URLError as e:
            sys.exit(f"Cannot reach OpenObserve at {self.base}: {e.reason}. Is it running? (make up)")

    def send(self, batch: Batch, repo: str) -> None:
        by_service: dict[str, list] = {}
        for s in batch.spans:
            by_service.setdefault(s.service, []).append(s.otlp())
        self.post("traces", {"resourceSpans": [
            {"resource": resource(svc, repo, self.env),
             "scopeSpans": [{"scope": {"name": "com.anthropic.claude_code.tracing" if svc == "claude-code"
                                       else "mcp-server"}, "spans": spans}]}
            for svc, spans in by_service.items()]})
        # Log events go through the _json API with the same flattened field names that OTLP
        # ingestion produces. (OpenObserve's OTLP/JSON logs endpoint rejects doubleValue
        # attributes, and cost_usd must stay a float for the dashboards.)
        res = {k.replace(".", "_"): v for k, v in _resource_dict("claude-code", repo, self.env).items()}
        rows = []
        for t, tid, sid, ev, a in batch.logs:
            row = {"_timestamp": t // 1000, "body": f"claude_code.{ev}", "severity": "INFO",
                   "trace_id": tid, "span_id": sid, **res}
            row.update({k.replace(".", "_"): v for k, v in a.items() if v is not None})
            rows.append(row)
        self.post(f"{self.stream}/_json", rows, base=self.api)
        self.post("metrics", {"resourceMetrics": [{"resource": resource("claude-code", repo, self.env), "scopeMetrics": [{
            "scope": {"name": "com.anthropic.claude_code"},
            "metrics": [
                {"name": "claude_code.cost.usage", "unit": "USD", "sum": {
                    "aggregationTemporality": 1, "isMonotonic": True,
                    "dataPoints": [{"timeUnixNano": str(t), "startTimeUnixNano": str(t - NS), "asDouble": usd,
                                    "attributes": attrs({"model": m, "vcs.repository.name": r.split("/")[1], **METRIC_ATTRS})}
                                   for t, usd, m, r in batch.cost]}},
                {"name": "claude_code.token.usage", "unit": "tokens", "sum": {
                    "aggregationTemporality": 1, "isMonotonic": True,
                    "dataPoints": [{"timeUnixNano": str(t), "startTimeUnixNano": str(t - NS), "asInt": str(n),
                                    "attributes": attrs({"type": k, "model": m, "vcs.repository.name": r.split("/")[1],
                                                         **METRIC_ATTRS})}
                                   for t, n, k, m, r in batch.tokens]}},
            ]}]}]})


def load_env() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=6, help="history to backfill (default 6)")
    p.add_argument("--tasks", type=int, default=30, help="tasks in the backfill (default 30)")
    p.add_argument("--live", action="store_true", help="after the backfill, emit a new task every ~20 s")
    p.add_argument("--stream", default="claude_code", help="stream for traces/logs (default claude_code)")
    p.add_argument("--env", default="demo", help="deployment.environment (default demo)")
    p.add_argument("--seed", type=int, default=None, help="random seed for repeatable data")
    args = p.parse_args()

    load_env()
    email = os.environ.get("ZO_ROOT_USER_EMAIL")
    password = os.environ.get("ZO_ROOT_USER_PASSWORD")
    if not email or not password:
        sys.exit("Set ZO_ROOT_USER_EMAIL and ZO_ROOT_USER_PASSWORD (in .env).")
    auth = base64.b64encode(f"{email}:{password}".encode()).decode()
    exporter = Exporter(os.environ.get("O2_URL", "http://localhost:5080"), os.environ.get("O2_ORG", "default"),
                        auth, args.stream, args.env)
    rng = random.Random(args.seed)

    now = time.time_ns()
    start = now - int(args.hours * 3600 * NS)
    spacing = (now - start) // max(args.tasks, 1)
    sessions = [(str(uuid.uuid4()), DEVELOPERS[i % len(DEVELOPERS)]) for i in range(max(4, args.tasks // 6))]
    spans = logs = 0
    for i in range(args.tasks):
        prompt, steps = rng.choice(TASKS)
        t0 = start + i * spacing + rng.randint(0, max(1, spacing // 3))
        session_id, dev = rng.choice(sessions)
        task = Task(rng, t0, session_id, rng.choice(REPOS), i + 1, dev)
        batch = task.run(prompt, steps)
        if max(s.end for s in batch.spans) > now:  # keep the backfill in the past
            continue
        exporter.send(batch, task.repo)
        spans += len(batch.spans)
        logs += len(batch.logs)
    print(f"sent {args.tasks} tasks: {spans} spans, {logs} log events -> stream '{args.stream}'")
    print(f"open {os.environ.get('O2_URL', 'http://localhost:5080')} -> Traces -> stream {args.stream}")

    seq = args.tasks
    while args.live:
        seq += 1
        prompt, steps = rng.choice(TASKS)
        repo = rng.choice(REPOS)
        session_id, dev = rng.choice(sessions)
        task = Task(rng, time.time_ns(), session_id, repo, seq, dev)
        batch = task.run(prompt, steps)
        # Shift the whole task so it ends now: traces appear complete, never in the future.
        shift = max(s.end for s in batch.spans) - time.time_ns()
        for s in batch.spans:
            s.start -= shift
            s.end -= shift
        batch.logs = [(t - shift, *rest) for t, *rest in batch.logs]
        batch.cost = [(t - shift, *rest) for t, *rest in batch.cost]
        batch.tokens = [(t - shift, *rest) for t, *rest in batch.tokens]
        exporter.send(batch, repo)
        print(f"live: task {seq} '{prompt[:40]}' ({len(batch.spans)} spans)", flush=True)
        time.sleep(rng.uniform(12, 28))


if __name__ == "__main__":
    main()
