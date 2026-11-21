"""Adapter: the Transport protocol implemented with httpx (extra `http`)."""

from collections.abc import Mapping
from typing import Any

import httpx

from endurance_planner.transport import Response, TransportError

DEFAULT_TIMEOUT_S = 30.0


class HttpxTransport:
    """Real HTTP calls. `transport` is an optional httpx transport (e.g. httpx.MockTransport
    in tests); network failures become TransportError("<httpx error name>: <message>")."""

    def __init__(
        self, transport: httpx.BaseTransport | None = None, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Response:
        try:
            response = self._client.request(
                method,
                url,
                params=dict(params) if params else None,
                json=json,
                headers=dict(headers or {}),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TransportError as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        return Response(response.status_code, dict(response.headers), response.text)

    def close(self) -> None:
        self._client.close()
