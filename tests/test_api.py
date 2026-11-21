"""HTTP API (FastAPI TestClient, temporary data dir, fake intervals.icu: no network)."""

import datetime as dt
import hmac
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from builders import EXAMPLE, TODAY
from endurance_planner import api, cli
from endurance_planner.export.intervals_icu import IntervalsClient
from endurance_planner.services import PlannerService
from endurance_planner.storage import FileStorage
from endurance_planner.transport import Response
from fake_intervals import FakeIntervals

TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "sk-intervals-SECRET-4242"


def _data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "profile.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return data


def _app(
    data: Path,
    *,
    fake: FakeIntervals | None = None,
    today: dt.date = TODAY,
    **kwargs: Any,
) -> TestClient:
    factory: Callable[[], IntervalsClient] | None = None
    if fake is not None:
        transport = fake.transport

        def factory() -> IntervalsClient:
            return IntervalsClient(SECRET, transport=transport, sleep=lambda _: None)

    app = api.create_app(
        data,
        token=TOKEN,
        today=lambda: today,
        client_factory=factory,
        env={},
        web_dir=None,
        **kwargs,
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def fake() -> FakeIntervals:
    return FakeIntervals()


@pytest.fixture
def client(tmp_path: Path, fake: FakeIntervals) -> Iterator[TestClient]:
    with _app(_data_dir(tmp_path), fake=fake) as test_client:
        yield test_client


def _generate(client: TestClient) -> dict[str, Any]:
    response = client.post("/plan/generate", json={"expected_revision": 0}, headers=AUTH)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def _key_date(client: TestClient, week: int) -> str:
    sessions = client.get(f"/plan/weeks/{week}", headers=AUTH).json()["sessions"]
    return str(next(s["date"] for s in sessions if s["hard"]))


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


# ---------------------------------------------------------------------- auth
ROUTES = [
    ("GET", "/profile/summary", None),
    ("GET", "/plan", None),
    ("GET", "/plan/weeks/1", None),
    ("GET", "/plan/amendments", None),
    ("GET", "/openapi.json", None),
    ("POST", "/plan/generate", {"expected_revision": 0}),
    ("POST", "/plan/amend", {"expected_revision": 0, "date": "2026-12-01", "skip": True}),
    ("POST", "/plan/unavailable", {"expected_revision": 0, "from": "2026-12-01"}),
    ("POST", "/plan/replan", {"expected_revision": 0}),
    ("POST", "/plan/amendments/a1/undo", {"expected_revision": 0}),
    ("POST", "/plan/push", {}),
]


def test_health_is_the_only_open_route(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200 and health.json() == {"status": "ok", "version": "0.1.0"}
    for method, path, body in ROUTES:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401, (method, path, headers)
            assert _code(response) == "unauthorized"
            assert response.headers["www-authenticate"] == "Bearer"
    assert client.get("/docs").status_code == 404  # Swagger UI would load from a CDN


def test_token_compared_in_constant_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)  # the module api.py calls
    assert (
        client.get("/plan/amendments", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    assert client.get("/profile/summary", headers=AUTH).status_code == 200
    assert calls == [(b"nope", TOKEN.encode()), (TOKEN.encode(), TOKEN.encode())]


def test_openapi_is_published_with_the_token(client: TestClient) -> None:
    schema = client.get("/openapi.json", headers=AUTH).json()
    assert {"/plan", "/plan/amend", "/plan/push", "/plan/weeks/{index}"} <= set(schema["paths"])
    assert "MutationResult" in schema["components"]["schemas"]


# ---------------------------------------------------------------------- reading
def test_profile_plan_and_weeks(client: TestClient) -> None:
    assert _code(client.get("/plan", headers=AUTH)) == "no_plan"
    summary = client.get("/profile/summary", headers=AUTH).json()
    assert summary["athlete"] == "Victor" and summary["zones"]
    created = _generate(client)
    assert (created["revision"], created["applied"], created["dry_run"]) == (0, True, False)
    overview = client.get("/plan", headers=AUTH)
    assert overview.headers["cache-control"] == "no-store"
    assert overview.headers["x-content-type-options"] == "nosniff"
    data = overview.json()
    assert (data["id"], data["revision"], data["current_week"], len(data["weeks"])) == (
        "victor-20261116",
        0,
        1,
        29,
    )
    week = client.get("/plan/weeks/3", headers=AUTH).json()
    assert week["index"] == 3 and week["sessions"]
    step = next(line for s in week["sessions"] for line in s["steps"] if line["absolute"])
    assert step["target"] and (" W" in step["absolute"] or "/km" in step["absolute"])
    missing = client.get("/plan/weeks/99", headers=AUTH)
    assert missing.status_code == 404 and _code(missing) == "unknown_week"
    assert "weeks 1 to 29" in missing.json()["error"]["message"]
    assert client.get("/plan/weeks/x", headers=AUTH).status_code == 422


# ---------------------------------------------------------------------- mutations
def test_dry_run_then_amend_then_stale_revision(client: TestClient, tmp_path: Path) -> None:
    _generate(client)
    plan_file = tmp_path / "data" / "plans" / "victor-20261116.json"
    before = plan_file.read_bytes()
    date = _key_date(client, 4)
    body = {"expected_revision": 0, "date": date, "minutes": 30, "reason": "short on time"}
    preview = client.post("/plan/amend", json={**body, "dry_run": True}, headers=AUTH).json()
    assert preview["dry_run"] and preview["applied"] and preview["revision"] == 0
    assert preview["changes"][0]["code"] == "amended" and preview["changes"][0]["after"]["pinned"]
    assert preview["message"] == "amendment a1: 30 min (scope rest)"
    assert plan_file.read_bytes() == before  # nothing stored
    done = client.post("/plan/amend", json=body, headers=AUTH).json()
    assert (done["dry_run"], done["revision"], done["changed"]) == (False, 1, True)
    assert plan_file.read_bytes() != before
    assert client.get("/plan", headers=AUTH).json()["revision"] == 1
    for dry_run in (True, False):
        stale = client.post("/plan/amend", json={**body, "dry_run": dry_run}, headers=AUTH)
        assert stale.status_code == 409 and _code(stale) == "revision_conflict"
        error = stale.json()["error"]
        assert (error["current_revision"], error["expected_revision"]) == (1, 0)
        assert "changed elsewhere" in error["message"]


def test_amend_errors(client: TestClient) -> None:
    _generate(client)
    rest_day = client.post(
        "/plan/amend",
        json={"expected_revision": 0, "date": "2026-11-23", "minutes": 30},
        headers=AUTH,
    )
    assert rest_day.status_code == 404 and _code(rest_day) == "unknown_date"
    assert "no session on 2026-11-23" in rest_day.json()["error"]["message"]
    combined = client.post(
        "/plan/amend",
        json={"expected_revision": 0, "date": _key_date(client, 2), "skip": True, "minutes": 3},
        headers=AUTH,
    )
    assert combined.status_code == 422 and _code(combined) == "amend_refused"
    assert "cannot be combined" in combined.json()["error"]["message"]
    for bad in (
        {"date": "2026-12-01"},
        {"expected_revision": 0, "date": "x"},
        {"expected_revision": 0, "date": "2026-12-01", "nope": 1},
    ):
        invalid = client.post("/plan/amend", json=bad, headers=AUTH)
        assert invalid.status_code == 422 and _code(invalid) == "invalid_request"
        assert "Traceback" not in invalid.text
    week = client.get("/plan/weeks/6", headers=AUTH).json()
    long_run = next(s for s in week["sessions"] if s["slot"] == "long")
    body = {"expected_revision": 0, "date": long_run["date"], "minutes": 240}
    refused = client.post("/plan/amend", json=body, headers=AUTH)
    assert refused.status_code == 422 and _code(refused) == "rules_broken"
    assert "long_session_ramp" in {i["code"] for i in refused.json()["error"]["issues"]}
    assert client.get("/plan", headers=AUTH).json()["revision"] == 0
    preview = client.post("/plan/amend", json={**body, "dry_run": True}, headers=AUTH)
    assert preview.status_code == 200 and preview.json()["refused"]  # previews show the errors
    forced = client.post("/plan/amend", json={**body, "force": True}, headers=AUTH)
    assert forced.status_code == 200 and forced.json()["revision"] == 1


def test_past_days_are_refused(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    with _app(data) as client:
        _generate(client)
        date = _key_date(client, 2)
    with _app(data, today=dt.date(2026, 12, 1)) as later:
        past = later.post(
            "/plan/amend", json={"expected_revision": 0, "date": date, "skip": True}, headers=AUTH
        )
    assert past.status_code == 422 and _code(past) == "amend_refused"
    assert "past days cannot be amended" in past.json()["error"]["message"]


def test_unavailable_replan_amendments_undo(client: TestClient, tmp_path: Path) -> None:
    _generate(client)
    body = {"expected_revision": 0, "from": "2026-12-21", "to": "2026-12-27", "minutes": {"run": 0}}
    done = client.post("/plan/unavailable", json=body, headers=AUTH).json()
    assert done["revision"] == 1 and done["amendment"]["kind"] == "unavailable"
    assert {c["code"] for c in done["changes"]} >= {"calendar_exception"}
    race = client.post(
        "/plan/unavailable", json={"expected_revision": 1, "from": "2027-03-14"}, headers=AUTH
    )
    assert race.status_code == 422 and "inside this off range" in race.json()["error"]["message"]
    listed = client.get("/plan/amendments", headers=AUTH).json()
    assert [(a["id"], a["active"]) for a in listed] == [("a1", True)]
    assert listed[0]["line"].startswith("a1   unavailable 2026-12-21 to 2026-12-27")
    replanned = client.post("/plan/replan", json={"expected_revision": 1}, headers=AUTH).json()
    assert replanned["revision"] == 1 and replanned["changes"] == []
    undo = client.post(
        "/plan/amendments/a1/undo", json={"expected_revision": 1, "dry_run": True}, headers=AUTH
    )
    assert undo.status_code == 200 and "undone" in {c["code"] for c in undo.json()["changes"]}
    undone = client.post("/plan/amendments/a1/undo", json={"expected_revision": 1}, headers=AUTH)
    assert (
        undone.json()["revision"] == 2 and client.get("/plan/amendments", headers=AUTH).json() == []
    )
    unknown = client.post("/plan/amendments/a9/undo", json={"expected_revision": 2}, headers=AUTH)
    assert unknown.status_code == 422 and "no amendment a9" in unknown.json()["error"]["message"]
    # A goal moved after the start: the weeks already done no longer match.
    profile = tmp_path / "data" / "profile.yaml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("date: 2027-03-14", "date: 2027-03-21"),
        encoding="utf-8",
    )
    with _app(tmp_path / "data", today=dt.date(2027, 1, 20)) as later:
        moved = later.post("/plan/replan", json={"expected_revision": 2}, headers=AUTH)
    assert moved.status_code == 422 and _code(moved) == "skeleton_changed"


def test_push(client: TestClient, fake: FakeIntervals, tmp_path: Path) -> None:
    _generate(client)
    dry = client.post("/plan/push", json={"from": "2026-11-23", "to": "2026-11-29"}, headers=AUTH)
    report = dry.json()
    assert (
        dry.status_code == 200 and report["dry_run"] and report["events"] == len(report["payloads"])
    )
    assert fake.transport.requests == []  # a dry run sends nothing
    pushed = client.post("/plan/push", json={"dry_run": False}, headers=AUTH).json()
    assert pushed["upserted"] == len(fake.events) > 0 and pushed["payloads"] == []
    assert pushed["threshold_diffs"][0] == "bike FTP (W): profile 260, intervals.icu 265"
    again = client.post("/plan/push", json={"dry_run": False}, headers=AUTH).json()
    assert again["upserted"] == 0 and again["unchanged"] == pushed["upserted"]
    fake.transport.handler = lambda request: Response(401, text="bad key")
    failed = client.post("/plan/push", json={"dry_run": False}, headers=AUTH)
    assert failed.status_code == 502 and _code(failed) == "intervals_error"
    assert "authentication failed" in failed.json()["error"]["message"]
    with _app(tmp_path / "data") as no_key:
        unconfigured = no_key.post("/plan/push", json={"dry_run": False}, headers=AUTH)
    assert unconfigured.status_code == 503 and _code(unconfigured) == "intervals_not_configured"


# ---------------------------------------------------------------------- errors and secrets
def test_no_secret_in_responses_or_logs(
    tmp_path: Path, fake: FakeIntervals, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    with _app(_data_dir(tmp_path), fake=fake) as client:
        _generate(client)
        date = _key_date(client, 4)
        calls = [
            ("GET", "/health", None),
            *ROUTES,
            ("POST", "/plan/amend", {"expected_revision": 0, "date": date, "minutes": 30}),
            ("POST", "/plan/push", {"dry_run": False}),
            ("POST", "/plan/push", {"dry_run": True}),
            ("GET", "/plan/weeks/0", None),
        ]
        for method, path, body in calls:
            response = client.request(method, path, json=body, headers=AUTH)
            seen.append(response.text + str(dict(response.headers)))
        fake.transport.handler = lambda request: Response(500, text="down")
        seen.append(client.post("/plan/push", json={"dry_run": False}, headers=AUTH).text)
    assert any("Authorization" in str(r.headers) for r in fake.transport.requests)  # key used
    for text in [*seen, caplog.text]:
        assert SECRET not in text and TOKEN not in text


def test_server_side_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _data_dir(tmp_path)
    with _app(data, lock_timeout_s=0.05) as client:
        _generate(client)
        with FileStorage(data).lock():
            busy = client.post("/plan/replan", json={"expected_revision": 0}, headers=AUTH)
        assert busy.status_code == 503 and _code(busy) == "busy"
        assert busy.headers["retry-after"] == "1"
        monkeypatch.setattr(PlannerService, "overview", lambda self, today: 1 / 0)
        crash = client.get("/plan", headers=AUTH)
        assert crash.status_code == 500 and _code(crash) == "internal_error"
        assert "ZeroDivision" not in crash.text and "Traceback" not in crash.text
        (data / "profile.yaml").write_text("athlete: [", encoding="utf-8")
        broken = client.get("/profile/summary", headers=AUTH)
        assert broken.status_code == 503 and _code(broken) == "profile_invalid"
        assert "invalid YAML" in broken.json()["error"]["message"]
        assert str(tmp_path) not in broken.text + busy.text  # no server path in messages


# ---------------------------------------------------------------------- server settings
def test_server_settings() -> None:
    with pytest.raises(api.ServerConfigError, match="PLANNER_TOKEN is not set"):
        api.check_server_settings(None, "127.0.0.1", insecure_local=False)
    with pytest.raises(api.ServerConfigError, match="only works with --host 127.0.0.1"):
        api.check_server_settings(None, "0.0.0.0", insecure_local=True)
    with pytest.raises(api.ServerConfigError, match="too short"):
        api.check_server_settings("short", "127.0.0.1", insecure_local=False)
    api.check_server_settings(None, "127.0.0.1", insecure_local=True)
    api.check_server_settings(TOKEN, "100.64.0.1", insecure_local=False)
    with pytest.raises(api.ServerConfigError):
        api.create_app(Path("."), token=None)


def test_insecure_local_app_needs_no_token(tmp_path: Path) -> None:
    app = api.create_app(_data_dir(tmp_path), token=None, insecure_local=True, env={}, web_dir=None)
    with TestClient(app) as client:
        assert client.get("/profile/summary").status_code == 200


def test_serve_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    started: list[tuple[Path, str, int, bool]] = []
    monkeypatch.setattr(api, "serve", lambda *args: started.append(args))
    monkeypatch.delenv("PLANNER_TOKEN", raising=False)
    monkeypatch.delenv("PLANNER_DATA_DIR", raising=False)
    nowhere = runner.invoke(cli.app, ["serve"])
    assert nowhere.exit_code == 1 and "PLANNER_DATA_DIR" in nowhere.output
    data = _data_dir(tmp_path)
    no_token = runner.invoke(cli.app, ["serve", "--data-dir", str(data)])
    assert no_token.exit_code == 1 and "PLANNER_TOKEN is not set" in no_token.output
    wide = runner.invoke(
        cli.app, ["serve", "--data-dir", str(data), "--host", "0.0.0.0", "--insecure-local"]
    )
    assert wide.exit_code == 1 and "only works with --host 127.0.0.1" in wide.output
    local = runner.invoke(cli.app, ["serve", "--data-dir", str(data), "--insecure-local"])
    assert local.exit_code == 0, local.output
    monkeypatch.setenv("PLANNER_TOKEN", TOKEN)
    monkeypatch.setenv("PLANNER_DATA_DIR", str(data))
    from_env = runner.invoke(cli.app, ["serve", "--port", "8123"])
    assert from_env.exit_code == 0, from_env.output
    assert started == [(data, "127.0.0.1", 8000, True), (data, "127.0.0.1", 8123, False)]
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = runner.invoke(cli.app, ["serve", "--data-dir", str(empty)])
    assert missing.exit_code == 1 and "profile.yaml is missing" in missing.output
