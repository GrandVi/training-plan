"""HTTP behind a small protocol, so that the core never depends on an HTTP library.

The core (export/intervals_icu.py) builds requests and reads responses through `Transport`.
Implementations: `http_transport.HttpxTransport` (adapter, extra `http`) for real calls, and
`FakeTransport` here, an in-memory one for tests and dry runs.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class TransportError(Exception):
    """The request got no answer (connection refused, timeout...). The message starts with
    the kind of failure, e.g. "ReadTimeout: ..."."""


@dataclass(frozen=True)
class Request:
    method: str
    url: str  # absolute URL, without the query string
    params: Mapping[str, str] | None = None
    json: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    text: str = ""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """The body parsed as JSON (None when empty)."""
        return json.loads(self.text) if self.text else None


@runtime_checkable
class Transport(Protocol):
    """Sends one HTTP request and returns its response; raises TransportError when there is
    no response. Retries, authentication and error messages belong to the caller."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Response: ...

    def close(self) -> None: ...


Handler = Callable[[Request], Response]


class FakeTransport:
    """In-memory transport: every request is recorded and answered by `handler`, which may
    also raise TransportError to simulate a network failure."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[Request] = []

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
        request = Request(method, url, dict(params) if params else None, json, dict(headers or {}))
        self.requests.append(request)
        return self.handler(request)

    def close(self) -> None:
        pass


def json_response(data: Any, status: int = 200) -> Response:
    """A JSON response, for fakes."""
    return Response(status, {"content-type": "application/json"}, json.dumps(data))
