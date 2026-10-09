"""Markdown rendering helpers and the size cap applied to every tool response."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

MAX_OUTPUT_CHARS = 8000
MAX_DATA_CHARS = 16000
TRUNCATION_NOTE = "\n\n_…truncated — narrow the window or lower the limit._"


@dataclass
class ToolOutput:
    markdown: str
    data: dict[str, Any] = field(default_factory=dict)

    def capped(self, limit: int = MAX_OUTPUT_CHARS) -> ToolOutput:
        text, text_cut = cap_text(self.markdown, limit)
        data, data_cut = cap_data(self.data)
        if text_cut or data_cut:
            data["truncated"] = True
        return ToolOutput(text, data)


def cap_text(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """Cut at a line boundary so the text plus the note fits in `limit` characters."""
    if len(text) <= limit:
        return text, False
    budget = limit - len(TRUNCATION_NOTE)
    cut = text.rfind("\n", 0, budget)
    return text[: cut if cut > 0 else budget] + TRUNCATION_NOTE, True


def cap_data(data: dict[str, Any], limit: int = MAX_DATA_CHARS) -> tuple[dict[str, Any], bool]:
    """Halve the longest top-level list until the JSON fits in `limit` characters."""
    out, cut = dict(data), False
    while len(json.dumps(out, default=str)) > limit:
        lists = [k for k, v in out.items() if isinstance(v, list) and v]
        if not lists:
            break
        key = max(lists, key=lambda k: len(out[k]))
        out[key] = out[key][: len(out[key]) // 2]
        cut = True
    return out, cut


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return fmt_num(value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "–"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def fmt_us_ts(us: Any, tz: dt.tzinfo = dt.UTC) -> str:
    """Format an OpenObserve _timestamp (µs) as 'YYYY-MM-DD HH:MM'."""
    value = to_float(us)
    if value is None:
        return "–"
    return dt.datetime.fromtimestamp(value / 1_000_000, tz).strftime("%Y-%m-%d %H:%M")


def to_float(value: Any) -> float | None:
    """Parse numbers that OpenObserve may return as strings (Utf8 attributes)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def num(value: Any) -> float:
    parsed = to_float(value)
    return 0.0 if parsed is None else parsed


def heading(title: str, subtitle: str = "") -> str:
    return f"## {title}" + (f"\n_{subtitle}_" if subtitle else "")


def empty(title: str, hint: str) -> ToolOutput:
    return ToolOutput(f"{heading(title)}\n\nNo data. {hint}", {"rows": []})
