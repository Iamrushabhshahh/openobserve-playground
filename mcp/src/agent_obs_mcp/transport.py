"""Streamable HTTP transport for sharing one server with a team, guarded by a bearer token."""

from __future__ import annotations

import hmac
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

TOKEN_ENV = "AGENT_OBS_HTTP_TOKEN"
HOSTS_ENV = "AGENT_OBS_ALLOWED_HOSTS"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class BearerAuth(BaseHTTPMiddleware):
    """Reject any request without `Authorization: Bearer <token>` (constant-time compare)."""

    def __init__(self, app: Starlette, token: str):
        super().__init__(app)
        self._expected = f"Bearer {token}".encode()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, self._expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_http_app(
    mcp: FastMCP, token: str | None, host: str, allowed_hosts: list[str] | None = None
) -> Starlette:
    """Streamable HTTP app; off loopback, a token and allowed Host headers are required."""
    if host not in LOOPBACK:
        if not token:
            raise SystemExit(
                f"refusing to listen on {host} without {TOKEN_ENV}; set a token or bind 127.0.0.1"
            )
        if not allowed_hosts:
            raise SystemExit(
                f"set {HOSTS_ENV} (e.g. obs.internal:8766) so DNS-rebinding protection stays on"
            )
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts
        )
    app = mcp.streamable_http_app()
    if token:
        app.add_middleware(BearerAuth, token=token)
    return app


def serve_http(mcp: FastMCP, host: str, port: int) -> None:
    import uvicorn

    hosts = [h.strip() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()]
    app = build_http_app(mcp, os.environ.get(TOKEN_ENV) or None, host, hosts)
    uvicorn.run(app, host=host, port=port, log_level="warning")
