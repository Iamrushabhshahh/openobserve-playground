"""Thin async client for OpenObserve's HTTP API (_search plus generic JSON calls)."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from .config import Settings
from .timewin import TimeRange

StreamType = Literal["logs", "traces", "metrics"]
MAX_ERROR_CHARS = 300


class O2Error(RuntimeError):
    """A request failed; the message is safe to show to the model (no credentials)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class O2Client:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport

    async def search(
        self, sql: str, stream_type: StreamType, time_range: TimeRange, size: int
    ) -> list[dict[str, Any]]:
        url = f"{self.settings.url}/api/{self.settings.org}/_search"
        body = {
            "query": {
                "sql": sql,
                "start_time": time_range.start_us,
                "end_time": time_range.end_us,
                "from": 0,
                "size": size,
            }
        }
        payload = await self._send("POST", url, params={"type": stream_type}, body=body)
        if not isinstance(payload, dict):
            raise O2Error("OpenObserve returned an unexpected response shape")
        hits = payload.get("hits", [])
        if not isinstance(hits, list):
            raise O2Error("OpenObserve returned an unexpected response (no 'hits' list)")
        return [h for h in hits if isinstance(h, dict)]

    async def request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        """Call any OpenObserve API. `path` is relative to /api/{org}/ unless it starts with '/'."""
        if path.startswith("/"):
            url = f"{self.settings.url}{path}"
        else:
            url = f"{self.settings.url}/api/{self.settings.org}/{path}"
        return await self._send(method, url, params=params, body=body)

    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self.settings.timeout_s
            ) as http:
                resp = await http.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers=self.settings.auth_headers(),
                )
        except httpx.TimeoutException as exc:
            raise O2Error(
                f"OpenObserve at {self.settings.safe_url} timed out after "
                f"{self.settings.timeout_s:.0f}s; narrow the window"
            ) from exc
        except httpx.HTTPError as exc:
            raise O2Error(
                f"cannot reach OpenObserve at {self.settings.safe_url} ({type(exc).__name__})"
            ) from exc
        return _parse_response(resp)


def _parse_response(resp: httpx.Response) -> Any:
    if resp.status_code in (401, 403):
        raise O2Error(
            f"OpenObserve rejected the credentials (HTTP {resp.status_code}); "
            "check O2_USER/O2_PASSWORD or O2_TOKEN",
            resp.status_code,
        )
    if resp.status_code >= 400:
        raise O2Error(
            f"OpenObserve request failed (HTTP {resp.status_code}): {_error_text(resp)}",
            resp.status_code,
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise O2Error("OpenObserve returned a non-JSON response") from exc
    return payload


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        text = (
            str(data.get("message") or data.get("error") or data)
            if isinstance(data, dict)
            else str(data)
        )
    except ValueError:
        text = resp.text
    return text[:MAX_ERROR_CHARS]
