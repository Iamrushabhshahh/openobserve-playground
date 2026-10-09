"""Relative time windows ("24h", "7d") to OpenObserve microsecond ranges."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

US_PER_S = 1_000_000
MAX_WINDOW_DAYS = 365

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86_400, "w": 7 * 86_400}
_WINDOW = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)


class WindowError(ValueError):
    """Raised for a window string that cannot be parsed or is out of range."""


@dataclass(frozen=True)
class TimeRange:
    start_us: int
    end_us: int

    @property
    def seconds(self) -> float:
        return (self.end_us - self.start_us) / US_PER_S


def parse_window(window: str) -> int:
    """Return the window length in seconds for strings like '90m', '24h', '7d', '2w'."""
    match = _WINDOW.match(window or "")
    if not match:
        raise WindowError(f"window must look like 30m, 24h, 7d or 2w; got {window!r}")
    amount, unit = int(match.group(1)), match.group(2).lower()
    seconds = amount * _UNIT_SECONDS[unit]
    if seconds <= 0:
        raise WindowError("window must be greater than zero")
    if seconds > MAX_WINDOW_DAYS * 86_400:
        raise WindowError(f"window is capped at {MAX_WINDOW_DAYS}d")
    return seconds


def window_range(window: str, now_s: float | None = None) -> TimeRange:
    end_s = time.time() if now_s is None else now_s
    end_us = int(end_s * US_PER_S)
    return TimeRange(start_us=end_us - parse_window(window) * US_PER_S, end_us=end_us)


def days_range(days: int, now_s: float | None = None) -> TimeRange:
    if days < 1 or days > MAX_WINDOW_DAYS:
        raise WindowError(f"days must be between 1 and {MAX_WINDOW_DAYS}")
    return window_range(f"{days}d", now_s)
