"""FastMCP wiring: tools, resources and prompts. Entry point: `agent-obs-mcp` (stdio)."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from . import agent_tools, completions, parity_tools, prompts, resources, sre_tools
from . import claude_tools as ct
from . import sql_tool as sq
from .client import O2Client
from .config import Settings, load_settings
from .deps import Deps
from .toolkit import INSTRUCTIONS, READ_ONLY, TraceId, Window, run_tool


def build_server(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> FastMCP:
    settings = settings or load_settings()
    deps = Deps(settings=settings, client=O2Client(settings, transport), clock=clock)
    mcp = FastMCP("agent-obs", instructions=INSTRUCTIONS)
    _register_claude_tools(mcp, deps)
    _register_other_tools(mcp, deps)
    parity_tools.register(mcp, deps)
    agent_tools.register(mcp, deps)
    sre_tools.register(mcp, deps)
    _register_resources(mcp, settings)
    _register_prompts(mcp)
    _register_completions(mcp, deps)
    return mcp


def _register_claude_tools(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(annotations=READ_ONLY)
    async def list_sessions(
        window: Window = "7d",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        sort: Literal["cost", "llm_calls", "duration"] = "cost",
    ) -> CallToolResult:
        """Claude Code sessions with cost, LLM calls, tool results/failures, models and first/last seen."""
        return await run_tool(ct.list_sessions(deps, window, limit, sort))

    @tool(annotations=READ_ONLY)
    async def get_trace_tree(
        trace_id: TraceId,
        max_spans: Annotated[int, Field(ge=1, le=2000)] = 500,
        window: Window = "30d",
    ) -> CallToolResult:
        """Span tree of one interaction with self time, critical path, top-5 self-time spans and LLM/tool/permission-wait totals."""
        return await run_tool(ct.get_trace_tree(deps, trace_id, max_spans, window))

    @tool(annotations=READ_ONLY)
    async def permission_wait_report(window: Window = "30d") -> CallToolResult:
        """Time spent waiting for permission answers, by source/decision and by tool, plus candidate permissions.allow rules."""
        return await run_tool(ct.permission_wait_report(deps, window))

    @tool(annotations=READ_ONLY)
    async def cost_report(
        window: Window = "7d", by: Literal["model", "session", "day"] = "model"
    ) -> CallToolResult:
        """Claude Code spend (USD) and tokens from api_request events, grouped by model, session or UTC day."""
        return await run_tool(ct.cost_report(deps, window, by))

    @tool(annotations=READ_ONLY)
    async def cache_efficiency(window: Window = "7d") -> CallToolResult:
        """Prompt-cache hit rate per model: cache_read / (cache_read + input + cache_creation)."""
        return await run_tool(ct.cache_efficiency(deps, window))

    @tool(annotations=READ_ONLY)
    async def slowest_tools(
        window: Window = "7d", top: Annotated[int, Field(ge=1, le=50)] = 10
    ) -> CallToolResult:
        """p50/p95/max duration of Claude Code tool calls by tool_name, slowest p95 first."""
        return await run_tool(ct.slowest_tools(deps, window, top))

    @tool(annotations=READ_ONLY)
    async def failures(window: Window = "7d") -> CallToolResult:
        """Failed tool executions by tool and error_class (with an example trace_id) and API errors by status code."""
        return await run_tool(ct.failures(deps, window))

    @tool(annotations=READ_ONLY)
    async def subagent_fanout(trace_id: TraceId, window: Window = "30d") -> CallToolResult:
        """Per-agent LLM calls, tokens and time in one trace; flags work that continued after the interaction ended."""
        return await run_tool(ct.subagent_fanout(deps, trace_id, window))

    @tool(annotations=READ_ONLY)
    async def llm_latency(window: Window = "7d") -> CallToolResult:
        """Time-to-first-token and total duration p50/p95 per model."""
        return await run_tool(ct.llm_latency(deps, window))


def _register_other_tools(mcp: FastMCP, deps: Deps) -> None:
    tool = mcp.tool

    @tool(annotations=READ_ONLY)
    async def run_readonly_sql(
        sql: Annotated[
            str, Field(description="One SELECT over the claude_code (or codex) streams")
        ],
        stream_type: Literal["logs", "traces"] = "logs",
        window: Window = "24h",
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> CallToolResult:
        """Guarded escape hatch: a single read-only SELECT; LIMIT forced to ≤500; content columns blocked unless allowed."""
        return await run_tool(sq.run_readonly_sql(deps, sql, stream_type, window, limit))


def _register_resources(mcp: FastMCP, settings: Settings) -> None:
    @mcp.resource("agentobs://schema/claude-code", mime_type="text/markdown")
    def claude_code_schema() -> str:
        """Claude Code span hierarchy, columns, units and SQL gotchas."""
        return resources.claude_code_schema(settings)

    @mcp.resource("agentobs://about", mime_type="text/markdown")
    def about() -> str:
        """What this server adds on top of OpenObserve's built-in MCP server."""
        return resources.ABOUT


def _register_prompts(mcp: FastMCP) -> None:
    mcp.prompt(description="Find where the time and money went in one session")(
        prompts.investigate_slow_session
    )
    mcp.prompt(description="Find permission prompts worth turning into allow rules")(
        prompts.permission_friction_audit
    )
    mcp.prompt(description="One-page weekly review of Claude Code usage")(
        prompts.weekly_agent_review
    )


def _register_completions(mcp: FastMCP, deps: Deps) -> None:
    @mcp.completion()
    async def complete_argument(ref, argument, context):
        return await completions.complete(deps, ref, argument)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-obs-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
        return
    from .transport import serve_http

    serve_http(server, args.host, args.port)
