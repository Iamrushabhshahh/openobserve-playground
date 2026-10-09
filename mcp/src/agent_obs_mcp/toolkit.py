"""Shared tool plumbing: annotations, parameter types and the result wrapper used by every tool."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .client import O2Error
from .render import ToolOutput

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
INSTRUCTIONS = (
    "Agent observability over OpenObserve for Claude Code and Codex telemetry. "
    "Prefer the task-shaped tools (sessions, trace trees, permission waits, loops, SLOs, "
    "regressions, budgets, audit trails); use search_sql or run_readonly_sql only when none fits, "
    "after reading agentobs://schema/claude-code. Everything is read-only except create_alert "
    "and create_dashboard, which dry-run by default and write only when AGENT_OBS_ALLOW_WRITES=1. "
    "Windows look like 24h, 7d, 30d."
)

Window = Annotated[str, Field(description="Relative window such as 24h, 7d, 30d, 2w")]
TraceId = Annotated[str, Field(description="trace_id of one Claude Code interaction")]


async def run_tool(work: Awaitable[ToolOutput]) -> CallToolResult:
    """Cap the output and turn expected failures into clean tool errors (no stack traces)."""
    try:
        out = (await work).capped()
    except (O2Error, ValueError) as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {exc}")], isError=True
        )
    return CallToolResult(
        content=[TextContent(type="text", text=out.markdown)], structuredContent=out.data
    )


WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
