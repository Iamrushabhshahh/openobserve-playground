from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_obs_mcp.client import O2Client
from agent_obs_mcp.config import load_settings
from agent_obs_mcp.deps import Deps

FIXTURES = Path(__file__).parent / "fixtures"
NOW_S = 1_791_460_800.0  # 2026-10-08 12:00 UTC
TEST_ENV = {"O2_URL": "http://o2.test:5080", "O2_USER": "me@example.com", "O2_PASSWORD": "s3cr3t!"}


def load_fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text())


class FakeO2:
    """Routes a search to canned hits by the first SQL substring that matches."""

    def __init__(self, routes: list[tuple[str, list[dict[str, Any]]]], status: int = 200):
        self.routes = routes
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(
            {"type": request.url.params.get("type"), "headers": request.headers, **body["query"]}
        )
        if self.status != 200:
            return httpx.Response(self.status, json={"code": self.status, "message": "nope"})
        sql = body["query"]["sql"]
        for needle, hits in self.routes:
            if needle in sql:
                return httpx.Response(200, json={"hits": hits, "total": len(hits)})
        return httpx.Response(200, json={"hits": [], "total": 0})

    def sqls(self) -> list[str]:
        return [r["sql"] for r in self.requests]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def make_deps() -> Callable[..., tuple[Deps, FakeO2]]:
    def factory(
        routes: list[tuple[str, list[dict[str, Any]]]] | None = None,
        status: int = 200,
        **env: str,
    ) -> tuple[Deps, FakeO2]:
        fake = FakeO2(routes or [], status)
        settings = load_settings({**TEST_ENV, **env})
        client = O2Client(settings, httpx.MockTransport(fake))
        return Deps(settings=settings, client=client, clock=lambda: NOW_S), fake

    return factory
