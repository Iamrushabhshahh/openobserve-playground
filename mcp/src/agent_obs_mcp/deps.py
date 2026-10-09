"""Shared dependencies for tool implementations, plus SQL literal helpers."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .client import O2Client, O2Error, StreamType
from .config import Settings
from .timewin import TimeRange

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
IN_CHUNK = 200


@dataclass
class Deps:
    settings: Settings
    client: O2Client
    clock: Callable[[], float] = field(default=time.time)

    @property
    def claude(self) -> str:
        return quote_ident(self.settings.claude_stream)

    async def query(
        self, sql: str, stream_type: StreamType, time_range: TimeRange, size: int = 1000
    ) -> list[dict[str, Any]]:
        return await self.client.search(sql, stream_type, time_range, size)

    async def query_optional(
        self, sql: str, stream_type: StreamType, time_range: TimeRange, size: int = 1000
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Like query, but a failure (e.g. a stream that does not exist yet) becomes a note."""
        try:
            return await self.query(sql, stream_type, time_range, size), None
        except O2Error as exc:
            return [], str(exc)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def require_id(name: str, value: str) -> str:
    if not _ID.match(value or ""):
        raise ValueError(f"{name} must be 1-128 characters of letters, digits, '_', '-', '.', ':'")
    return value


def in_list(values: Iterable[str]) -> str:
    return ", ".join(sql_str(v) for v in values)


def chunks(values: list[str], size: int = IN_CHUNK) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))
