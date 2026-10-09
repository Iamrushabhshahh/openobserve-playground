"""Guard for the run_readonly_sql escape hatch: one SELECT over allow-listed streams."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

MAX_LIMIT = 500

CONTENT_COLUMNS = frozenset(
    {
        "user_prompt",
        "prompt",
        "tool_input",
        "tool_parameters",
        "full_command",
        "bash_command",
        "file_path",
        "arguments",
        "output",
        "tool_output",
        "error_message",
        "response",
        "content",
        "body",
        "message_content",
        "completion",
    }
)

_FORBIDDEN = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "create",
        "alter",
        "truncate",
        "grant",
        "revoke",
        "merge",
        "copy",
        "attach",
        "detach",
        "pragma",
        "set",
        "into",
        "call",
        "execute",
        "exec",
        "vacuum",
        "upsert",
    }
)

_STRING = re.compile(r"'(?:[^']|'')*'")
_QUOTED_IDENT = re.compile(r'"((?:[^"]|"")*)"')
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SOURCE = re.compile(r'\b(?:from|join)\s+("(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_.]*|\()', re.I)
_FROM_INSIDE_CALL = re.compile(r"\b(?:extract|substring|trim|overlay|position)\s*\([^()]*\)", re.I)
_TRAILING_LIMIT = re.compile(r"\blimit\s+(\d+)\s*(?:offset\s+\d+\s*)?$", re.I)


class SqlGuardError(ValueError):
    """The query was rejected; the message says why."""


@dataclass(frozen=True)
class GuardedSql:
    sql: str
    limit: int
    streams: tuple[str, ...]


def guard_sql(
    sql: str,
    is_allowed_stream: Callable[[str], bool],
    allow_content: bool,
    limit: int = 200,
) -> GuardedSql:
    """Validate a user query and force a LIMIT ≤ MAX_LIMIT; raise SqlGuardError on rejection."""
    text = (sql or "").strip()
    if not text:
        raise SqlGuardError("empty query")
    masked = _STRING.sub(_blank, text)
    _reject_structure(masked)
    _reject_keywords(masked)
    streams = _check_streams(masked, is_allowed_stream)
    if not allow_content:
        _reject_content_columns(masked)
    bounded = max(1, min(int(limit), MAX_LIMIT))
    return GuardedSql(sql=_force_limit(text, masked, bounded), limit=bounded, streams=streams)


def strip_content(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop content-bearing keys from result rows (covers SELECT *)."""
    return [{k: v for k, v in row.items() if not is_content_column(k)} for row in rows]


def is_content_column(name: str) -> bool:
    low = name.lower()
    return low in CONTENT_COLUMNS or low.endswith("_prompt") or low.endswith("_content")


def _blank(match: re.Match[str]) -> str:
    """Length-preserving mask so offsets in the masked text map back to the original."""
    return "'" + " " * (len(match.group(0)) - 2) + "'"


def _reject_structure(masked: str) -> None:
    if ";" in masked:
        raise SqlGuardError("';' is not allowed: send a single statement")
    if "--" in masked or "/*" in masked:
        raise SqlGuardError("SQL comments are not allowed")
    if not re.match(r"^select\b", masked, re.I):
        raise SqlGuardError("only a single SELECT statement is allowed")


def _reject_keywords(masked: str) -> None:
    unquoted = _QUOTED_IDENT.sub('""', masked)
    for word in _WORD.findall(unquoted):
        if word.lower() in _FORBIDDEN:
            raise SqlGuardError(f"keyword {word.upper()} is not allowed (read-only)")


def _check_streams(masked: str, is_allowed: Callable[[str], bool]) -> tuple[str, ...]:
    streams: list[str] = []
    for match in _SOURCE.finditer(_FROM_INSIDE_CALL.sub(" ", masked)):
        token = match.group(1)
        if token == "(":
            continue
        name = token[1:-1].replace('""', '"') if token.startswith('"') else token
        if not is_allowed(name):
            raise SqlGuardError(f"stream {name!r} is not in the allow-list")
        streams.append(name)
    if not streams:
        raise SqlGuardError("query must read FROM an allow-listed stream")
    return tuple(dict.fromkeys(streams))


def _reject_content_columns(masked: str) -> None:
    idents = _WORD.findall(_QUOTED_IDENT.sub(" ", masked))
    idents += _QUOTED_IDENT.findall(masked)
    for name in idents:
        if is_content_column(name):
            raise SqlGuardError(
                f"column {name!r} holds prompt/tool content and is blocked; "
                "set AGENT_OBS_ALLOW_CONTENT=1 to allow it"
            )


def _force_limit(text: str, masked: str, bounded: int) -> str:
    match = _TRAILING_LIMIT.search(masked)
    if match:
        effective = min(int(match.group(1)), bounded)
        return f"{text[: match.start()].rstrip()} LIMIT {effective}"
    return f"{text} LIMIT {bounded}"
