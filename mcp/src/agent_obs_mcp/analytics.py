"""Permission-wait aggregations joined in Python (OpenObserve SQL avoids self-joins)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .render import num

ALLOW_MIN_COUNT = 5
ALLOW_MIN_RATE = 0.95


@dataclass
class ToolWait:
    tool: str
    waits: int = 0
    total_us: float = 0.0
    max_us: float = 0.0
    accepts: int = 0
    rejects: int = 0
    manual_accepts: int = 0

    @property
    def manual_rate(self) -> float:
        return self.manual_accepts / self.waits if self.waits else 0.0

    def add(self, row: dict[str, Any]) -> None:
        duration = num(row.get("duration"))
        decision = str(row.get("decision") or "")
        source = str(row.get("source") or "")
        self.waits += 1
        self.total_us += duration
        self.max_us = max(self.max_us, duration)
        if decision == "accept":
            self.accepts += 1
            if source.startswith("user"):
                self.manual_accepts += 1
        elif decision == "reject":
            self.rejects += 1


def aggregate_waits_by_tool(
    blocked: Iterable[dict[str, Any]], parent_tool: dict[str, str]
) -> list[ToolWait]:
    """Group blocked_on_user spans by tool, falling back to the parent tool span's tool_name."""
    by_tool: dict[str, ToolWait] = {}
    for row in blocked:
        tool = (
            str(row.get("tool_name") or "")
            or parent_tool.get(str(row.get("reference_parent_span_id") or ""), "")
            or "(unknown)"
        )
        by_tool.setdefault(tool, ToolWait(tool)).add(row)
    return sorted(by_tool.values(), key=lambda t: t.total_us, reverse=True)


def allow_rule_candidates(
    tools: Sequence[ToolWait],
    min_count: int = ALLOW_MIN_COUNT,
    min_rate: float = ALLOW_MIN_RATE,
) -> list[dict[str, Any]]:
    """Tools the user approves by hand almost every time: candidates for permissions.allow."""
    out = []
    for t in tools:
        if t.tool == "(unknown)" or t.manual_accepts < min_count or t.manual_rate < min_rate:
            continue
        out.append(
            {
                "tool": t.tool,
                "rule": suggested_rule(t.tool),
                "manual_accepts": t.manual_accepts,
                "waits": t.waits,
                "manual_rate": round(t.manual_rate, 3),
                "wait_minutes": round(t.total_us / 60_000_000, 1),
            }
        )
    return out


def suggested_rule(tool: str) -> str:
    if tool == "Bash":
        return "Bash(<command prefix>:*)  — scope it to the commands you approve; never bare Bash"
    if tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return f"{tool}(./src/**)  — scope it to the paths you approve"
    return tool
