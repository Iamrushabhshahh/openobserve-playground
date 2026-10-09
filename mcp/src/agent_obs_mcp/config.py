"""Settings read from the environment. Credentials never leave this module except as a header."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORG = re.compile(r"^[A-Za-z0-9_-]+$")
_TRUTHY = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """Raised when an environment setting is invalid."""


@dataclass(frozen=True)
class Settings:
    url: str = "http://localhost:5080"
    org: str = "default"
    claude_stream: str = "claude_code"
    allow_content: bool = False
    allow_writes: bool = False
    codex_stream: str = "codex"
    tz_name: str = "Asia/Kolkata"
    timeout_s: float = 30.0
    _auth_header: str | None = field(default=None, repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def safe_url(self) -> str:
        """The base URL with any userinfo removed, safe to show in messages."""
        parts = urlsplit(self.url)
        host = parts.hostname or ""
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": self._auth_header} if self._auth_header else {}

    def is_allowed_stream(self, name: str) -> bool:
        return name in (self.claude_stream, self.codex_stream)


def _auth_from_env(env: Mapping[str, str]) -> str | None:
    token = env.get("O2_TOKEN", "").strip()
    if token:
        return token if token.lower().startswith("basic ") else f"Basic {token}"
    user, password = env.get("O2_USER", ""), env.get("O2_PASSWORD", "")
    if user and password:
        raw = base64.b64encode(f"{user}:{password}".encode()).decode()
        return f"Basic {raw}"
    return None


def _require_ident(name: str, value: str) -> str:
    if not _IDENT.match(value):
        raise ConfigError(f"{name} must be a plain identifier (letters, digits, _), got {value!r}")
    return value


def _require_org(value: str) -> str:
    if not _ORG.match(value):
        raise ConfigError(f"O2_ORG must contain only letters, digits, '_' or '-', got {value!r}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    tz_name = env.get("AGENT_OBS_TZ", "Asia/Kolkata")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"AGENT_OBS_TZ is not a known time zone: {tz_name!r}") from exc
    return Settings(
        url=env.get("O2_URL", "http://localhost:5080").rstrip("/"),
        org=_require_org(env.get("O2_ORG", "default")),
        claude_stream=_require_ident("CLAUDE_STREAM", env.get("CLAUDE_STREAM", "claude_code")),
        allow_content=env.get("AGENT_OBS_ALLOW_CONTENT", "").strip().lower() in _TRUTHY,
        allow_writes=env.get("AGENT_OBS_ALLOW_WRITES", "").strip().lower() in _TRUTHY,
        codex_stream=_require_ident("CODEX_STREAM", env.get("CODEX_STREAM", "codex")),
        tz_name=tz_name,
        timeout_s=float(env.get("O2_TIMEOUT_S", "30")),
        _auth_header=_auth_from_env(env),
    )
