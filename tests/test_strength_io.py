"""Strength sessions outside the library (F10): intervals.icu export, Markdown and CSV, services,
HTTP API, CLI and web app. Mocked HTTP only."""

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from builders import EXAMPLE, TODAY, make_profile, with_strength
from endurance_planner import api, cli, services
from endurance_planner.amend import StrengthEdit, amend_strength
from endurance_planner.export.intervals_icu import (
    clear_events,
    external_id,
    plan_events,
    push_plan,
    strength_payload,
)
from endurance_planner.export.markdown import STRENGTH_COLUMNS, render_markdown, weekly_csv
from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile
from endurance_planner.planner import generate_plan
from endurance_planner.serialization import dump_plan
from fake_intervals import FakeIntervals

TOKEN = "strength-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WEB = Path(api.__file__).parent / "web"


@pytest.fixture(scope="module")
def athlete() -> Profile:
    return with_strength(make_profile())


@pytest.fixture(scope="module")
def plan(athlete: Profile) -> Plan:
    return generate_plan(athlete, TODAY)


def strength_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == "WeightTraining"]


# ---------------------------------------------------------------------- intervals.icu
def test_weight_training_payload(plan: Plan) -> None:
    session = plan.weeks[0].strength[0]
    payload = strength_payload(plan.id, session)
    assert payload == {
        "category": "WORKOUT",
        "start_date_local": f"{session.date}T00:00:00",
        "type": "WeightTraining",
        "name": session.name,
        "description": payload["description"],
        "moving_time": session.minutes * 60,
        "icu_training_load": round(session.minutes / 60 * 35),
        "external_id": f"endurance-planner:{plan.id}:{session.date}:strength",
    }
    lines = payload["description"].splitlines()
    assert lines[0].startswith("1. ") and "reps in reserve" in lines[0]
    assert not any(line.startswith("-") or line.strip().endswith("x") for line in lines)
    assert external_id(plan.id, session).endswith(":strength")
    events = plan_events(plan, plan.weeks[0].start, plan.weeks[0].start + dt.timedelta(6))
    assert len(strength_events(events)) == len(plan.weeks[0].strength)


def test_push_is_idempotent_and_prunes_strength(plan: Plan, athlete: Profile) -> None:
    fake = FakeIntervals()
    client = fake.client()
    first = push_plan(client, plan, only_changed=True)
    count = len(plan_events(plan))
    assert first.upserted == count == len(fake.events)
    assert len(strength_events(list(fake.events.values()))) == sum(
        len(w.strength) for w in plan.weeks
    )
    again = push_plan(client, plan, only_changed=True)
    assert (again.upserted, again.unchanged, again.pruned) == (0, count, 0)
    session = plan.weeks[1].strength[0]
    result = amend_strength(plan, athlete, TODAY, session.date, StrengthEdit(skip=True))
    after = push_plan(client, result.plan, only_changed=True)
    rewritten = [c for c in result.changes if c.after is not None]  # rung held later on
    assert after.pruned == 1 and after.upserted == len(rewritten)
    gone = f"endurance-planner:{plan.id}:{session.date}:strength"
    assert gone not in {e["external_id"] for e in fake.events.values()}
    removed = clear_events(client, plan.weeks[0].start, plan.weeks[-1].start + dt.timedelta(6))
    assert strength_events(removed) and fake.events == {}


# ---------------------------------------------------------------------- Markdown and CSV
def test_markdown_and_csv_show_strength(plan: Plan, athlete: Profile) -> None:
    text = render_markdown(plan, athlete)
    assert "| Strength |" in text and "WeightTraining: Strength foundation A" in text
    assert "1 x" not in text.split("## Week 1")[0]  # the table shows counts and minutes
    session = plan.weeks[0].strength[0]
    assert f"- {session.exercises[0].label()}" in text
    rows = weekly_csv(plan, athlete).splitlines()
    assert rows[0].endswith(",".join(STRENGTH_COLUMNS))
    week = plan.weeks[0]
    assert rows[1].endswith(f",{len(week.strength)},{week.strength_minutes}")
    plain = generate_plan(make_profile(), TODAY)
    assert "Strength" not in weekly_csv(plain, make_profile()).splitlines()[0]
    assert "| Strength |" not in render_markdown(plain, make_profile())


# ---------------------------------------------------------------------- services
def test_services_show_strength(plan: Plan, athlete: Profile) -> None:
    week = services.show_week(plan, athlete, 1)
    assert [s.date for s in week.strength] == [s.date for s in plan.weeks[0].strength]
    first = week.strength[0]
    assert first.lines[0].startswith("1. ") and first.exercises and first.tss > 0
    assert week.strength_minutes == plan.weeks[0].strength_minutes
    overview = services.plan_overview(plan, athlete, TODAY)
    assert overview.weeks[0].strength_sessions == len(plan.weeks[0].strength)
    request = services.StrengthAmendRequest(date=first.date, equipment=[])
    outcome = services.amend_strength(plan, athlete, TODAY, request, expected_revision=0)
    result = outcome.result
    assert result.applied and result.amendment is not None
    kinds = {(c.kind, c.code) for c in result.changes}
    assert ("strength", "amended") in kinds
    change = next(c for c in result.changes if c.code == "amended")
    assert change.strength_after is not None and change.strength_after.equipment == ["bodyweight"]
    assert change.before is None and "Strength" in change.line
    with pytest.raises(services.RevisionConflict):
        services.amend_strength(plan, athlete, TODAY, request, expected_revision=3)


# ---------------------------------------------------------------------- HTTP API
@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    data = tmp_path / "data"
    data.mkdir()
    profile = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    profile["availability"]["strength"] = {"sessions_per_week": 2}
    (data / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    app = api.create_app(data, token=TOKEN, today=lambda: TODAY, env={}, web_dir=None)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_api_shows_and_amends_strength(client: TestClient) -> None:
    assert client.post("/plan/generate", json={"expected_revision": 0}, headers=AUTH).is_success
    week = client.get("/plan/weeks/1", headers=AUTH).json()
    assert week["strength"] and week["strength_minutes"] > 0
    session = week["strength"][0]
    assert session["lines"] and session["exercises"][0]["sets"] >= 1
    assert client.get("/plan", headers=AUTH).json()["weeks"][0]["strength_sessions"] == 2
    body = {"expected_revision": 0, "date": session["date"], "minutes": 20, "dry_run": True}
    dry = client.post("/plan/strength/amend", json=body, headers=AUTH)
    assert dry.status_code == 200 and dry.json()["dry_run"] and not dry.json()["refused"]
    assert dry.json()["changes"][0]["kind"] == "strength"
    applied = client.post("/plan/strength/amend", json={**body, "dry_run": False}, headers=AUTH)
    assert applied.status_code == 200 and applied.json()["revision"] == 1
    after = client.get("/plan/weeks/1", headers=AUTH).json()["strength"][0]
    assert after["pinned"] and after["minutes"] <= 20
    stale = client.post("/plan/strength/amend", json={**body, "dry_run": False}, headers=AUTH)
    assert stale.status_code == 409
    missing = {"expected_revision": 1, "date": "2026-11-16", "skip": True}
    response = client.post("/plan/strength/amend", json=missing, headers=AUTH)
    assert response.status_code == 404 and response.json()["error"]["code"] == "unknown_date"
    short = {"expected_revision": 1, "date": session["date"], "minutes": 10}
    response = client.post("/plan/strength/amend", json=short, headers=AUTH)
    assert response.status_code == 422
    bad = {"expected_revision": 1, "date": session["date"], "equipment": ["sofa"]}
    assert client.post("/plan/strength/amend", json=bad, headers=AUTH).status_code == 422
    unauthenticated = client.post("/plan/strength/amend", json=body)
    assert unauthenticated.status_code == 401
    log = client.get("/plan/amendments", headers=AUTH).json()
    assert log[0]["kind"] == "strength"


# ---------------------------------------------------------------------- CLI
def test_cli_amends_strength(tmp_path: Path, plan: Plan, athlete: Profile) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump(athlete.model_dump(mode="json", by_alias=True)))
    path = tmp_path / "plan.json"
    path.write_text(dump_plan(plan), encoding="utf-8")
    date = plan.weeks[1].strength[0].date.isoformat()
    args = ["plan", "amend", str(path), "--date", date, "--strength", "--profile", str(profile)]
    result = CliRunner().invoke(
        cli.app, [*args, "--equipment", "bodyweight,bands", "--today", TODAY.isoformat()]
    )
    assert result.exit_code == 0, result.output
    stored = json.loads(path.read_text())
    assert stored["amendments"][0]["kind"] == "strength"
    assert stored["amendments"][0]["equipment"] == ["bodyweight", "bands"]
    wrong = CliRunner().invoke(cli.app, [*args, "--sport", "run", "--today", TODAY.isoformat()])
    assert wrong.exit_code != 0 and "do not apply to strength" in wrong.output
    typo = CliRunner().invoke(cli.app, [*args, "--equipment", "sofa", "--today", "2026-11-16"])
    assert typo.exit_code != 0 and "--equipment" in typo.output


# ---------------------------------------------------------------------- web app
def test_web_app_shows_and_amends_strength() -> None:
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "week.strength" in script and '"/plan/strength/amend"' in script
    assert "#\\/strength\\/" in script and "#\\/change-strength\\/" in script
    assert 'icon("strength")' in script and 'change.kind === "strength"' in script
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="i-strength"' in page
    assert 'const VERSION = "v1"' not in (WEB / "sw.js").read_text(encoding="utf-8")
