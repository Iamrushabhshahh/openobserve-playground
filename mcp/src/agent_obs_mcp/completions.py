"""Argument completion for prompts: live session ids and common windows."""

from __future__ import annotations

from mcp.types import Completion, CompletionArgument, PromptReference, ResourceTemplateReference

from .client import O2Error
from .deps import Deps
from .timewin import window_range

MAX_VALUES = 20
WINDOWS = ("1h", "6h", "24h", "7d", "14d", "30d")


def _prefixed(values: list[str], prefix: str) -> Completion:
    hits = [v for v in values if v.startswith(prefix)]
    return Completion(values=hits[:MAX_VALUES], total=len(hits), hasMore=len(hits) > MAX_VALUES)


async def recent_session_ids(deps: Deps, prefix: str, window: str = "7d") -> list[str]:
    """Most recent session ids first; a lookup failure yields no suggestions, never an error."""
    rng = window_range(window, deps.clock())
    sql = (
        f"SELECT session_id, MAX(_timestamp) AS last_seen FROM {deps.claude} "
        "WHERE session_id IS NOT NULL GROUP BY session_id ORDER BY last_seen DESC"
    )
    try:
        rows = await deps.query(sql, "logs", rng, size=200)
    except O2Error:
        return []
    ids = [str(r["session_id"]) for r in rows if r.get("session_id")]
    return [i for i in ids if i.startswith(prefix)]


async def complete(
    deps: Deps,
    ref: PromptReference | ResourceTemplateReference,
    argument: CompletionArgument,
) -> Completion | None:
    if not isinstance(ref, PromptReference):
        return None
    name, value = argument.name, argument.value or ""
    if name == "session_id":
        return _prefixed(await recent_session_ids(deps, value), value)
    if name == "window":
        return _prefixed(list(WINDOWS), value)
    return None
