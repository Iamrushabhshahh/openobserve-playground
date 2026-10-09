"""Pure span analysis: tree building, self time, critical path, sub-agent fan-out."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .render import num, to_float

OP_INTERACTION = "claude_code.interaction"
OP_LLM = "claude_code.llm_request"
OP_TOOL = "claude_code.tool"
OP_BLOCKED = "claude_code.tool.blocked_on_user"
OP_EXEC = "claude_code.tool.execution"
MAIN_AGENT = "main"
NS_PER_MS = 1_000_000


@dataclass
class SpanNode:
    span_id: str
    parent_id: str
    operation: str
    detail: str
    start_ns: int
    end_ns: int
    agent_id: str = ""
    children: list[SpanNode] = field(default_factory=list)
    self_ms: float = 0.0

    @property
    def duration_ms(self) -> float:
        return max(0, self.end_ns - self.start_ns) / NS_PER_MS

    @property
    def short_op(self) -> str:
        return self.operation.removeprefix("claude_code.")

    def label(self) -> str:
        return f"{self.short_op} {self.detail}".strip()


@dataclass
class TraceTree:
    roots: list[SpanNode]
    nodes: dict[str, SpanNode]

    def main_root(self) -> SpanNode | None:
        return max(self.roots, key=lambda n: n.duration_ms, default=None)


def span_from_row(row: dict[str, Any]) -> SpanNode:
    start = int(num(row.get("start_time")))
    end = int(num(row.get("end_time")))
    if end <= start and to_float(row.get("duration")) is not None:
        end = start + int(num(row.get("duration")) * 1000)
    return SpanNode(
        span_id=str(row.get("span_id") or ""),
        parent_id=str(row.get("reference_parent_span_id") or ""),
        operation=str(row.get("operation_name") or ""),
        detail=str(row.get("tool_name") or row.get("model") or ""),
        start_ns=start,
        end_ns=end,
        agent_id=str(row.get("agent_id") or ""),
    )


def build_tree(rows: Iterable[dict[str, Any]]) -> TraceTree:
    nodes: dict[str, SpanNode] = {}
    for row in rows:
        node = span_from_row(row)
        if node.span_id:
            nodes[node.span_id] = node
    roots: list[SpanNode] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_id)
        if parent is None or parent is node:
            roots.append(node)
        else:
            parent.children.append(node)
    for node in nodes.values():
        node.children.sort(key=lambda c: (c.start_ns, c.end_ns))
        node.self_ms = self_time_ms(node)
    roots.sort(key=lambda r: r.start_ns)
    return TraceTree(roots=roots, nodes=nodes)


def self_time_ms(node: SpanNode) -> float:
    """Duration not covered by any child; overlapping (parallel) children count once."""
    intervals = sorted(
        (max(c.start_ns, node.start_ns), min(c.end_ns, node.end_ns)) for c in node.children
    )
    merged: list[list[int]] = []
    for start, end in intervals:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    covered = sum(end - start for start, end in merged)
    return max(0, node.end_ns - node.start_ns - covered) / NS_PER_MS


def critical_path(root: SpanNode | None) -> list[SpanNode]:
    """From the root, repeatedly follow the child that finishes last."""
    path: list[SpanNode] = []
    node = root
    while node is not None:
        path.append(node)
        node = max(node.children, key=lambda c: c.end_ns, default=None)
    return path


def walk(roots: Sequence[SpanNode]) -> Iterable[tuple[int, SpanNode]]:
    stack = [(0, r) for r in reversed(roots)]
    while stack:
        depth, node = stack.pop()
        yield depth, node
        stack.extend((depth + 1, c) for c in reversed(node.children))


def render_tree(tree: TraceTree, highlight: set[str]) -> list[str]:
    lines = []
    for depth, node in walk(tree.roots):
        mark = "★ " if node.span_id in highlight else ""
        lines.append(
            f"{'  ' * depth}- {mark}{node.label()} — {node.duration_ms:,.0f} ms "
            f"(self {node.self_ms:,.0f} ms)"
        )
    return lines


def totals(tree: TraceTree) -> dict[str, float | int]:
    by_op: dict[str, float] = {}
    for node in tree.nodes.values():
        by_op[node.operation] = by_op.get(node.operation, 0.0) + node.duration_ms
    agents = {n.agent_id for n in tree.nodes.values() if n.agent_id}
    return {
        "spans": len(tree.nodes),
        "llm_ms": round(by_op.get(OP_LLM, 0.0), 1),
        "tool_execution_ms": round(by_op.get(OP_EXEC, 0.0), 1),
        "blocked_on_user_ms": round(by_op.get(OP_BLOCKED, 0.0), 1),
        "subagents": len(agents),
    }


def top_self_time(tree: TraceTree, n: int = 5) -> list[SpanNode]:
    return sorted(tree.nodes.values(), key=lambda s: s.self_ms, reverse=True)[:n]


@dataclass
class AgentStats:
    agent_id: str
    llm_calls: int = 0
    tokens: int = 0
    llm_ms: float = 0.0
    first_ns: int | None = None
    last_ns: int | None = None

    def add_span(self, row: dict[str, Any], node: SpanNode) -> None:
        self.first_ns = (
            node.start_ns if self.first_ns is None else min(self.first_ns, node.start_ns)
        )
        self.last_ns = node.end_ns if self.last_ns is None else max(self.last_ns, node.end_ns)
        if node.operation != OP_LLM:
            return
        self.llm_calls += 1
        self.llm_ms += node.duration_ms
        self.tokens += int(
            sum(
                num(row.get(k))
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                )
            )
        )


def fanout(rows: Sequence[dict[str, Any]]) -> tuple[list[AgentStats], int | None]:
    """Per-agent stats and the end (ns) of the interaction span, if present."""
    agents: dict[str, AgentStats] = {}
    interaction_end: int | None = None
    for row in rows:
        node = span_from_row(row)
        if node.operation == OP_INTERACTION:
            interaction_end = max(interaction_end or 0, node.end_ns)
            continue
        key = node.agent_id or MAIN_AGENT
        agents.setdefault(key, AgentStats(key)).add_span(row, node)
    ordered = sorted(agents.values(), key=lambda a: (a.agent_id != MAIN_AGENT, a.first_ns or 0))
    return ordered, interaction_end


def overrun_ms(agent: AgentStats, interaction_end: int | None) -> float:
    if interaction_end is None or agent.last_ns is None:
        return 0.0
    return max(0, agent.last_ns - interaction_end) / NS_PER_MS
