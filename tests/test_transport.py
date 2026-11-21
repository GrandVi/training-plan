"""HTTP behind the Transport protocol: the client logic with a fake transport, and the
httpx adapter. No network."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from endurance_planner.export import intervals_icu
from endurance_planner.export.intervals_icu import IntervalsClient, IntervalsError
from endurance_planner.http_transport import HttpxTransport
from endurance_planner.settings import environment, intervals_settings, load_env_file
from endurance_planner.transport import (
    FakeTransport,
    Request,
    Response,
    Transport,
    TransportError,
    json_response,
)


def _client(
    handler: object, **kwargs: object
) -> tuple[IntervalsClient, FakeTransport, list[float]]:
    sleeps: list[float] = []
    fake = FakeTransport(handler)  # type: ignore[arg-type]
    client = IntervalsClient("secret", transport=fake, sleep=sleeps.append, **kwargs)  # type: ignore[arg-type]
    return client, fake, sleeps


def test_requests_go_through_the_transport() -> None:
    client, fake, _ = _client(lambda request: json_response([{"id": 7}]))
    assert client.upsert_events([{"external_id": "x"}]) == [{"id": 7}]
    [request] = fake.requests
    assert request == Request(
        "POST",
        "https://intervals.icu/api/v1/athlete/0/events/bulk",
        {"upsert": "true"},
        [{"external_id": "x"}],
        {"Authorization": "Basic " + base64.b64encode(b"API_KEY:secret").decode()},
    )
    assert isinstance(fake, Transport)


def test_retries_and_messages_are_unchanged() -> None:
    answers: list[Response | Exception] = [
        TransportError("ReadTimeout: slow"),
        Response(429, text="later"),
        json_response({"id": "i1"}),
    ]

    def handler(request: Request) -> Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    logs: list[str] = []
    client, fake, sleeps = _client(handler, log=logs.append)
    assert client.athlete() == {"id": "i1"}
    assert sleeps == [1.0, 2.0] and len(fake.requests) == 3
    assert any("retry 1/4 in 1 s (ReadTimeout: slow)" in line for line in logs)
    assert any(line.startswith("> GET /athlete/0") for line in logs)
    assert not any("secret" in line for line in logs)
    refused, _, _ = _client(lambda request: Response(401, text="nope"))
    with pytest.raises(IntervalsError, match="authentication failed.* - nope"):
        refused.athlete()


def test_httpx_adapter_maps_network_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = HttpxTransport(httpx.MockTransport(handler))
    with pytest.raises(TransportError, match="^ConnectError: refused$"):
        transport.request("GET", "https://example.invalid/x")
    ok = HttpxTransport(httpx.MockTransport(lambda r: httpx.Response(201, json={"a": [1]})))
    response = ok.request("PUT", "https://example.invalid/y", params={"q": "1"}, json={"b": 2})
    assert (response.status, response.json(), response.is_success) == (201, {"a": [1]}, True)
    ok.close()


def test_settings_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text('INTERVALS_API_KEY="from-file"\nINTERVALS_ATHLETE_ID=i9\n', encoding="utf-8")
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    monkeypatch.delenv("INTERVALS_ATHLETE_ID", raising=False)
    assert intervals_settings(environment(str(env))).api_key == "from-file"
    monkeypatch.setenv("INTERVALS_API_KEY", "from-env")
    settings = intervals_settings(environment(str(env)), athlete_id="i5")
    assert (settings.api_key, settings.athlete_id) == ("from-env", "i5")
    assert intervals_settings({}).athlete_id == "0"
    # Still importable from its old module.
    assert intervals_icu.load_env_file is load_env_file
    assert json.loads(json_response({"x": 1}).text) == {"x": 1}
