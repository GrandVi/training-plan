"""Activity sync and plan review (F7): recorded intervals.icu JSON, no network."""

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from builders import EXAMPLE, TODAY
from endurance_planner import api, cli, services
from endurance_planner.config import load_profile
from endurance_planner.export.json_io import load_history, save_history
from endurance_planner.history import (
    merge_history,
    parse_activity,
    parse_records,
    parse_wellness,
    sync_window,
)
from endurance_planner.models.enums import Sport
from endurance_planner.models.history import DayObservation, History
from endurance_planner.models.plan import Plan
from endurance_planner.planner import generate_plan
from endurance_planner.review import review_csv, review_markdown, review_plan
from endurance_planner.serialization import dump_history, parse_history
from fake_intervals import FakeIntervals

FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITIES: list[dict[str, Any]] = json.loads((FIXTURES / "intervals_activities.json").read_text())
WELLNESS: list[dict[str, Any]] = json.loads((FIXTURES / "intervals_wellness.json").read_text())
NOW = dt.date(2026, 11, 26)  # Thursday of week 2
NOV_17 = dt.date(2026, 11, 17)
TOKEN = "history-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def plan() -> Plan:
    return generate_plan(load_profile(EXAMPLE), TODAY)


def synced() -> History:
    parsed = parse_records(ACTIVITIES, WELLNESS)
    return merge_history(History(), parsed.observations, parsed.wellness, synced=NOW)[0]


# ---------------------------------------------------------------------- parsing
def test_activities_are_parsed_with_their_fallbacks() -> None:
    by_id = {o.activity_id: o for o in parse_records(ACTIVITIES, []).observations}
    run = by_id["i1001"]
    assert (run.date, run.sport, run.duration_s, run.tss) == (
        dt.date(2026, 11, 17),
        Sport.RUN,
        2040,
        40,
    )
    assert run.intensity_factor == 0.85 and run.zone_times_s == (600, 700, 300, 240, 200)
    assert not run.tss_estimated and run.category == "run"
    assert by_id["i1002"].zone_times_s == (1200, 3600, 600)  # power zones as {"id", "secs"}
    trail = by_id["i1005"]  # no load: duration x IF^2 (1.333 h x 0.78^2 x 100 = 81.1)
    assert (trail.tss, trail.tss_estimated, trail.zone_times_s) == (81.1, True, (1200, 3000, 600))
    assert by_id["i1006"].intensity_factor == 0.693  # IF from load: sqrt(80 / 166.7)
    gym = by_id["i1007"]  # strength: flat 35 TSS per hour
    assert (gym.sport, gym.category, gym.tss, gym.intensity_factor) == (
        None,
        "strength",
        14.6,
        None,
    )
    assert by_id["i1004"].category == "other" and by_id["i1004"].tss == 25
    assert by_id["i1011"].tss == 36.0  # unknown type, nothing given: IF 0.6 for 1 h
    assert by_id["i1003"].sport is Sport.BIKE  # VirtualRide


def test_unknown_types_and_unreadable_records_are_counted() -> None:
    parsed = parse_records(ACTIVITIES, WELLNESS)
    assert dict(parsed.unknown_types) == {"Swim": 1, "Yoga": 1}
    assert dict(parsed.categories) == {"run": 3, "bike": 4, "strength": 1, "other": 2}
    assert parsed.unreadable == 2  # an activity without a date, a wellness record without one
    assert parse_activity({"start_date_local": "2026-11-17"}) is None  # no id
    assert parse_activity({"id": 1, "start_date_local": "not a date"}) is None


def test_wellness_parsing_is_tolerant() -> None:
    days = {d.date: d for d in parse_records([], WELLNESS).wellness}
    first = days[dt.date(2026, 11, 17)]
    assert (first.hrv, first.resting_hr, first.sleep_s, first.sleep_score) == (62.5, 48, 27000, 82)
    assert (first.ctl, first.atl) == (45.2, 50.1)
    second = days[dt.date(2026, 11, 18)]  # "n/a", null and a negative value are ignored
    assert (second.hrv, second.resting_hr, second.ctl, second.atl) == (None, None, 46.0, None)
    assert days[dt.date(2026, 11, 19)].hrv == 58  # alternative name
    assert parse_wellness({"id": "2026-11-20", "hrv": True}) is not None  # never fails
    assert parse_wellness({"id": 20261120}) is None


def test_merge_is_idempotent_and_keeps_old_observations() -> None:
    history = synced()
    parsed = parse_records(ACTIVITIES, WELLNESS)
    again, counts = merge_history(history, parsed.observations, parsed.wellness, synced=NOW)
    assert again == history and (counts.added, counts.updated, counts.wellness) == (0, 0, 0)
    assert counts.unchanged == len(parsed.observations)
    edited = [o for o in parsed.observations if o.activity_id == "i1009"]
    longer = DayObservation(**{**edited[0].__dict__, "duration_s": 3300})
    merged, counts = merge_history(history, [longer], [])
    assert (counts.added, counts.updated) == (0, 1)
    assert len(merged.observations) == len(history.observations)  # nothing dropped
    assert parse_history(dump_history(merged)) == merged


def test_sync_window() -> None:
    assert sync_window(History(), NOW) == NOW - dt.timedelta(days=28)
    assert sync_window(History(synced=NOW), NOW) == NOW - dt.timedelta(days=7)
    assert sync_window(History(synced=NOW), NOW, dt.date(2026, 11, 1)) == dt.date(2026, 11, 1)


# ---------------------------------------------------------------------- sync (mocked HTTP)
def test_sync_twice_changes_nothing(tmp_path: Path) -> None:
    fake = FakeIntervals(ACTIVITIES, WELLNESS)
    history, report = services.sync_history(History(), fake.client(), NOW, since=TODAY)
    assert (report.activities, report.added, report.updated, report.stored) == (10, 10, 0, 10)
    assert report.unknown_types == {"Swim": 1, "Yoga": 1} and report.unreadable == 2
    assert report.wellness_days == 3
    assert any("Swim x1" in line for line in report.lines())
    path = tmp_path / "history.json"
    save_history(history, path)
    text = path.read_text()
    again, second = services.sync_history(load_history(path), fake.client(), NOW, since=TODAY)
    assert (second.added, second.updated, second.unchanged) == (0, 0, 10)
    save_history(again, path)
    assert path.read_text() == text
    dry, report = services.sync_history(History(), fake.client(), NOW, dry_run=True)
    assert dry == History() and report.dry_run
    requests = [r for r in fake.transport.requests if r.method == "GET"]
    assert {r.url.rsplit("/", 1)[1] for r in requests} == {"activities", "wellness"}
    assert requests[0].params == {"oldest": "2026-11-16", "newest": "2026-11-26"}


def test_history_file_missing_is_empty(tmp_path: Path) -> None:
    assert load_history(tmp_path / "none.json") == History()
    bad = tmp_path / "future.json"
    bad.write_text(json.dumps({"schema_version": 9}))
    with pytest.raises(ValueError, match="schema_version 9"):
        load_history(bad)


# ---------------------------------------------------------------------- review
def test_review_reproduces_hand_computed_totals(plan: Plan) -> None:
    review = review_plan(plan, synced(), NOW)
    assert [w.index for w in review.weeks] == [1, 2]  # weeks started by today
    first, second = review.weeks
    assert first.finished and not second.finished
    assert (first.total.planned_s, first.total.planned_tss) == (22710, 305.7)
    assert (first.total.actual_s, first.total.actual_tss) == (25140, 347.7)
    cat = first.by_category
    assert (cat["run"].actual_s, cat["run"].actual_tss) == (6840, 121.1)
    assert (cat["bike"].actual_s, cat["bike"].actual_tss) == (15000, 187.0)
    assert (cat["strength"].actual_s, cat["other"].actual_s) == (1500, 1800)
    assert first.other_types == {"Swim": 1}
    assert [(k.status, k.completion) for k in first.keys] == [("done", 1.01)]
    assert (first.planned_hard, first.actual_hard, first.missed_keys) == (1, 1, 0)
    assert first.low_share == 0.859  # 13600 s of 15840 s in zones 1-2
    assert [(k.status, k.completion) for k in second.keys] == [("partial", 0.5)]
    assert (second.total.actual_s, second.total.actual_tss, second.actual_hard) == (8100, 106, 1)
    assert second.other_types == {"Yoga": 1} and second.low_share is None
    assert review.outside == 0


def test_review_selection_and_missed_keys(plan: Plan) -> None:
    empty = review_plan(plan, History(), NOW)
    assert [k.status for k in empty.weeks[0].keys] == ["missed"]
    assert empty.weeks[0].missed_keys == 1
    assert [w.index for w in review_plan(plan, synced(), NOW, week=5).weeks] == [5]
    upcoming = review_plan(plan, synced(), NOW, week=5).weeks[0].keys
    assert {k.status for k in upcoming} == {"upcoming"}
    since = review_plan(plan, synced(), NOW, since=dt.date(2026, 11, 23))
    assert [w.index for w in since.weeks] == [2]
    with pytest.raises(services.UnknownWeek):
        services.review_plan(plan, synced(), NOW, week=99)


def test_review_text_forms(plan: Plan) -> None:
    review = review_plan(plan, synced(), NOW)
    text = review_markdown(review)
    assert "| 1 | 2026-11-16 | base | 6.3 / 7.0 | 306 / 348 |" in text
    assert "Swim x1" in text and "Yoga x1" in text
    assert "Tue 24 Nov run" in text and ": partial (25m of 50m, 50%)" in text
    rows = review_csv(review).splitlines()
    assert rows[0].startswith("week,start,phase,planned_h,actual_h")
    assert rows[1].split(",")[:7] == ["1", "2026-11-16", "base", "6.3", "7.0", "306", "348"]
    assert services.render_review(review, "csv") == review_csv(review)
    with pytest.raises(services.UnknownFormat):
        services.render_review(review, "pdf")


# ---------------------------------------------------------------------- CLI
def test_cli_sync_and_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan: Plan) -> None:
    fake = FakeIntervals(ACTIVITIES, WELLNESS)
    monkeypatch.setattr(cli, "make_client", lambda athlete_id, verbose: fake.client())
    history = tmp_path / "history.json"
    args = ["sync", "--history", str(history), "--since", "2026-11-16", "--today", "2026-11-26"]
    runner = CliRunner()
    first = runner.invoke(cli.app, args)
    assert first.exit_code == 0, first.output
    assert "10 new" in first.output and "Swim x1, Yoga x1" in first.output
    stored = history.read_text()
    second = runner.invoke(cli.app, args)
    assert "0 new, 0 updated, 10 unchanged" in second.output and history.read_text() == stored
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2))
    review = runner.invoke(
        cli.app,
        ["plan", "review", str(plan_path), "--history", str(history), "--today", "2026-11-26"],
    )
    assert review.exit_code == 0 and "| 2 | 2026-11-23 |" in review.output
    csv_out = runner.invoke(
        cli.app,
        [
            "plan",
            "review",
            str(plan_path),
            "--history",
            str(history),
            "-f",
            "csv",
            "--week",
            "1",
            "--today",
            "2026-11-26",
        ],
    )
    assert csv_out.output.count("\n") == 2
    both = runner.invoke(
        cli.app, ["plan", "review", str(plan_path), "--week", "1", "--since", "2026-11-20"]
    )
    assert both.exit_code != 0


# ---------------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, dt.date]]]:
    data = tmp_path / "data"
    data.mkdir()
    (data / "profile.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    fake = FakeIntervals(ACTIVITIES, WELLNESS)
    clock = {"today": TODAY}
    app = api.create_app(
        data,
        token=TOKEN,
        today=lambda: clock["today"],
        client_factory=fake.client,
        env={},
        web_dir=None,
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, clock


def test_api_sync_and_review(client: tuple[TestClient, dict[str, dt.date]]) -> None:
    http, clock = client
    assert http.post("/history/sync", json={}).status_code == 401
    generated = http.post("/plan/generate", json={"expected_revision": 0}, headers=AUTH)
    assert generated.status_code == 200, generated.text
    clock["today"] = NOW
    dry = http.post("/history/sync", json={"since": "2026-11-16", "dry_run": True}, headers=AUTH)
    assert dry.status_code == 200 and dry.json()["added"] == 10
    assert http.get("/plan/review", headers=AUTH).json()["synced"] is None  # nothing stored
    done = http.post("/history/sync", json={"since": "2026-11-16"}, headers=AUTH).json()
    assert done["added"] == 10 and done["unknown_types"] == {"Swim": 1, "Yoga": 1}
    again = http.post("/history/sync", json={"since": "2026-11-16"}, headers=AUTH).json()
    assert (again["added"], again["updated"], again["unchanged"]) == (0, 0, 10)
    review = http.get("/plan/review", headers=AUTH).json()
    assert review["synced"] == "2026-11-26" and len(review["weeks"]) == 2
    assert review["weeks"][0]["total"]["actual_tss"] == 347.7
    one = http.get("/plan/review?week=2", headers=AUTH).json()
    assert [w["index"] for w in one["weeks"]] == [2]
    assert http.get("/plan/review?week=99", headers=AUTH).status_code == 404
    both = http.get("/plan/review?week=1&since=2026-11-20", headers=AUTH)
    assert both.status_code == 422


def test_api_sync_without_intervals_settings(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "profile.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    app = api.create_app(data, token=TOKEN, today=lambda: NOW, env={}, web_dir=None)
    with TestClient(app, raise_server_exceptions=False) as http:
        response = http.post("/history/sync", json={}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "intervals_not_configured"


# ---------------------------------------------------------------------- web app
def test_web_app_has_a_review_screen() -> None:
    web = Path(api.__file__).parent / "web"
    script = (web / "app.js").read_text(encoding="utf-8")
    assert '"/plan/review"' in script and '"/history/sync"' in script
    assert "showReview" in script and "#\\/review$" in script
    assert 'data-tab="review"' in (web / "index.html").read_text(encoding="utf-8")
