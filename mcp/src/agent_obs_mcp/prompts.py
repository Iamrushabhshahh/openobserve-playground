"""Prompt templates for common investigations."""

from __future__ import annotations

import re

_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def investigate_slow_session(session_id: str) -> str:
    session_id = _UNSAFE.sub("", session_id)[:128]
    return f"""Investigate why Claude Code session `{session_id}` was slow or expensive.

1. Call `list_sessions` (window "30d", sort "duration") and find this session's row:
   cost, LLM calls, tool failures, models, first/last seen.
2. Use `run_readonly_sql` (stream_type "traces") to list its interactions:
   SELECT trace_id, duration FROM "claude_code" WHERE session_id = '{session_id}'
   AND operation_name = 'claude_code.interaction' ORDER BY duration DESC
3. For the two slowest trace_ids call `get_trace_tree` and `subagent_fanout`.
4. Report where the time went (LLM vs tool execution vs waiting for permission),
   the critical path, the top self-time spans, and 2–3 concrete fixes
   (allow rules, smaller context, fewer sub-agents, faster tools)."""


def permission_friction_audit(window: str = "30d") -> str:
    return f"""Audit permission friction in Claude Code over the last {window}.

1. Call `permission_wait_report` with window "{window}".
2. Summarise total minutes spent waiting, split by source and decision.
3. Name the tools that cost the most waiting time.
4. For each suggested `permissions.allow` rule, say whether it is safe as-is or should
   be scoped (Bash and file-write tools should always be scoped to commands/paths).
5. Output a ready-to-paste `permissions.allow` JSON snippet for the safe ones only."""


def weekly_agent_review() -> str:
    return """Write a weekly review of my Claude Code usage (last 7 days).

Call these tools with window "7d": `cost_report` (by "day" and by "model"),
`cache_efficiency`, `llm_latency`, `slowest_tools`, `failures`, `list_sessions`
(sort "cost", limit 5) and `permission_wait_report`.

Then write: total spend and the trend by day; the most expensive sessions; cache hit
rate per model and whether it is healthy (>80% is good for long sessions); latency
outliers; the top failing tools with an example trace_id; permission friction; and the
three highest-leverage changes for next week. Keep it under one page."""
