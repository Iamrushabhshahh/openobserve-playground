from __future__ import annotations

import pytest
from mcp.types import CompletionArgument, PromptReference, ResourceTemplateReference
from starlette.testclient import TestClient

from agent_obs_mcp.completions import complete
from agent_obs_mcp.server import build_server
from agent_obs_mcp.transport import build_http_app

pytestmark = pytest.mark.anyio
PROMPT = PromptReference(type="ref/prompt", name="investigate_slow_session")


async def test_session_ids_complete_by_prefix(make_deps):
    rows = [{"session_id": "abc-1"}, {"session_id": "abd-2"}, {"session_id": "xyz-3"}]
    deps, fake = make_deps([("GROUP BY session_id", rows)])
    out = await complete(deps, PROMPT, CompletionArgument(name="session_id", value="ab"))
    assert out.values == ["abc-1", "abd-2"]
    assert "user_prompt" not in fake.requests[0]["sql"]


async def test_session_lookup_failure_gives_no_suggestions(make_deps):
    deps, _ = make_deps(status=500)
    out = await complete(deps, PROMPT, CompletionArgument(name="session_id", value=""))
    assert out.values == []


async def test_window_and_unknown(make_deps):
    deps, _ = make_deps()
    window = await complete(deps, PROMPT, CompletionArgument(name="window", value="1"))
    assert window.values == ["1h", "14d"]
    assert await complete(deps, PROMPT, CompletionArgument(name="other", value="")) is None
    ref = ResourceTemplateReference(type="ref/resource", uri="agentobs://about")
    assert await complete(deps, ref, CompletionArgument(name="window", value="")) is None


def test_http_requires_token_and_hosts_off_loopback():
    with pytest.raises(SystemExit):
        build_http_app(build_server(), token=None, host="0.0.0.0", allowed_hosts=["x:1"])
    with pytest.raises(SystemExit):
        build_http_app(build_server(), token="t", host="0.0.0.0", allowed_hosts=[])


def test_http_rejects_unlisted_host_header():
    app = build_http_app(build_server(), "t", "0.0.0.0", ["obs.internal:8766"])
    with TestClient(app, base_url="http://evil.example:8766") as client:
        resp = client.post("/mcp", json={}, headers={"Authorization": "Bearer t"})
        assert resp.status_code == 421


def test_http_rejects_missing_or_wrong_token():
    app = build_http_app(build_server(), "t0ken", "0.0.0.0", ["obs.internal:8766"])
    with TestClient(app, base_url="http://obs.internal:8766") as client:
        assert client.post("/mcp", json={}).status_code == 401
        bad = client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        ok = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
            headers={
                "Authorization": "Bearer t0ken",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert ok.status_code == 200
