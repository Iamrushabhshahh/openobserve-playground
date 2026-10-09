"""The guarded run_readonly_sql escape hatch."""

from __future__ import annotations

from typing import Any, cast

from .client import StreamType
from .deps import Deps
from .render import ToolOutput, heading, table
from .sqlguard import guard_sql, strip_content
from .timewin import window_range

MAX_COLUMNS = 20
MAX_CELL_CHARS = 120


async def run_readonly_sql(
    deps: Deps, sql: str, stream_type: str = "logs", window: str = "24h", limit: int = 200
) -> ToolOutput:
    if stream_type not in ("logs", "traces"):
        raise ValueError("stream_type must be 'logs' or 'traces'")
    settings = deps.settings
    guarded = guard_sql(sql, settings.is_allowed_stream, settings.allow_content, limit)
    rng = window_range(window, deps.clock())
    rows = await deps.query(guarded.sql, cast(StreamType, stream_type), rng, guarded.limit)
    if not settings.allow_content:
        rows = strip_content(rows)
    columns = _columns(rows)
    title = heading(f"{len(rows)} rows ({stream_type}, last {window})", guarded.sql)
    body = (
        table(columns, [[_clip(r.get(c)) for c in columns] for r in rows]) if rows else "No rows."
    )
    data = {"sql": guarded.sql, "stream_type": stream_type, "window": window, "rows": rows}
    return ToolOutput(f"{title}\n\n{body}", data)


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows[:50]:
        seen.update(dict.fromkeys(row))
    return list(seen)[:MAX_COLUMNS]


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    return value
