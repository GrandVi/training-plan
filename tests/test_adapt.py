"""plan adapt (F8): the plan rebuilt from what was done. No network."""

import datetime as dt
import random
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from builders import EXAMPLE, TODAY, make_profile, with_strength
from endurance_planner import api, cli, services
from endurance_planner.adapt import adapt_plan, adjust_plan, calendar_events, low_readiness
from endurance_planner.amend import AmendError
from endurance_planner.export.intervals_icu import plan_events
from endurance_planner.load import run_impact_multiplier, week_weighted_load
from endurance_planner.models.enums import Severity, Sport
from endurance_planner.models.history import DayObservation, History, WellnessDay
from endurance_planner.models.plan import Plan, Session, Week
from endurance_planner.models.profile import Profile
from endurance_planner.planner import generate_plan
from endurance_planner.replan import replan
from endurance_planner.rules import (
    OVERREACH_FACTOR,
    baseline_load,
    load_cap,
    planned_version,
    ref_load,
    reference,
)
from endurance_planner.serialization import dump_history, dump_plan
from endurance_planner.validation import validate_plan
from fake_intervals import FakeIntervals
from test_property import random_amendment, random_profile
from test_strength_plan import random_strength

NOW = TODAY + dt.timedelta(days=16)  # Wednesday of week 3
TOKEN = "adapt-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
Scale = Callable[[Session], float]


def history_from(plan: Plan, until: dt.date, scale: Scale = lambda s: 1.0) -> History:
    """Activities matching the planned sessions before `until`, scaled (0: not done)."""
    observations = []
    for week in plan.weeks:
        for session in week.sessions:
            factor = scale(session)
            if session.date >= until or factor <= 0:
                continue
            observations.append(
                DayObservation(
                    session.date,
                    session.sport,
                    int(session.duration_s * factor),
                    round(session.tss * factor, 1),
                    activity_id=f"{session.date}-{session.sport.value}",
                    activity_type="Run" if session.sport is Sport.RUN else "Ride",
                )
            )
    return History(synced=until, observations=observations)


def errors(plan: Plan, profile: Profile) -> list[str]:
    return [str(i) for i in validate_plan(plan, profile) if i.severity is Severity.ERROR]


def future(plan: Plan, day: dt.date) -> list[Session]:
    return [s for w in plan.weeks for s in w.sessions if s.date >= day]


@pytest.fixture(scope="module")
def setup() -> tuple[Plan, Profile]:
    profile = make_profile()
    return generate_plan(profile, TODAY), profile


# ---------------------------------------------------------------------- scenarios
def test_everything_done_keeps_the_plan(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    result = adapt_plan(plan, profile, history_from(plan, NOW), NOW)
    assert future(result.plan, NOW) == future(plan, NOW)
    assert {c.code for c in result.changes} == {"actual"}
    assert all(c.date < NOW for c in result.changes)
    done = [s for w in result.plan.weeks for s in w.sessions if s.actual]
    assert len(done) == len([s for w in plan.weeks for s in w.sessions if s.date < NOW])
    keys = [s for s in done if s.template_id]
    assert keys and all(s.rung is not None for s in keys)  # ladders advance
    assert result.issues == [] and errors(result.plan, profile) == []


def test_missed_week(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    second = plan.weeks[1]
    history = history_from(
        plan, NOW, lambda s: 0.0 if second.start <= s.date <= second.start + dt.timedelta(6) else 1
    )
    result = adapt_plan(plan, profile, history, NOW)
    codes = {c.code for c in result.changes}
    assert {"actual", "missed_key", "rebalanced"} <= codes
    notes = {i.code for i in result.issues}
    assert {"adapt_missed_key", "adapt_under_training"} <= notes
    impact = run_impact_multiplier(profile)
    weeks = result.plan.weeks
    loads = [ref_load(w, impact) for w in weeks[:2]]
    assert week_weighted_load(weeks[1], impact) == 0
    # no catch-up: week 3 stays at the reference before the missed week
    assert week_weighted_load(weeks[2], impact) <= reference(loads[:1], baseline_load(profile)) + 1
    missed_key = next(s for s in second.sessions if s.intent.hard)
    later = [s for s in future(result.plan, NOW) if s.template_id == missed_key.template_id]
    assert all(s.rung is None or s.rung <= (missed_key.rung or 0) for s in later[:1])
    assert errors(result.plan, profile) == []


def test_overreach_week_does_not_raise_the_caps(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    result = adapt_plan(plan, profile, history_from(plan, NOW, lambda s: 1.4), NOW)
    assert "adapt_overreach" in {i.code for i in result.issues}
    impact = run_impact_multiplier(profile)
    first = result.plan.weeks[0]
    planned = week_weighted_load(planned_version(first), impact)
    assert week_weighted_load(first, impact) > 1.3 * planned
    assert ref_load(first, impact) == pytest.approx(OVERREACH_FACTOR * planned)
    refs = [ref_load(w, impact) for w in result.plan.weeks[:2]]
    third = result.plan.weeks[2]
    assert week_weighted_load(third, impact) <= load_cap(refs, profile) + 0.5
    raw = [week_weighted_load(w, impact) for w in result.plan.weeks[:2]]
    assert load_cap(refs, profile) < load_cap(raw, profile)
    assert errors(result.plan, profile) == []


def test_ten_day_gap_is_a_break(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    gap = (TODAY + dt.timedelta(days=4), TODAY + dt.timedelta(days=13))
    history = history_from(plan, NOW, lambda s: 0.0 if gap[0] <= s.date <= gap[1] else 1)
    result = adapt_plan(plan, profile, history, NOW)
    assert "adapt_gap" in {i.code for i in result.issues}
    warnings = [w.code for w in result.plan.warnings]
    assert "return_from_break" in warnings
    assert errors(result.plan, profile) == []


def test_two_low_readiness_days_ease_the_next_key(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    wellness = [
        WellnessDay(NOW - dt.timedelta(days=offset), hrv=60.0) for offset in range(3, 30)
    ] + [
        WellnessDay(NOW - dt.timedelta(days=2), hrv=48.0),
        WellnessDay(NOW - dt.timedelta(1), hrv=50.0),
    ]
    history = history_from(plan, NOW).model_copy(update={"wellness": wellness})
    assert low_readiness(wellness, NOW) is not None
    off = adapt_plan(plan, profile, history, NOW)
    assert off.eased is None  # use_wellness is off by default
    data = profile.model_dump(mode="json", by_alias=True)
    data["preferences"]["adaptation"] = {"use_wellness": True}
    gated = Profile.model_validate(data)
    result = adapt_plan(plan, gated, history, NOW)
    assert result.eased is not None
    amendment = result.plan.amendments[-1]
    assert "low readiness" in amendment.reason and amendment.id == result.eased
    eased = next(s for w in result.plan.weeks for s in w.sessions if s.amendment_id == amendment.id)
    before = next(s for s in future(off.plan, NOW) if s.date == eased.date)
    assert eased.duration_s < before.duration_s or not eased.intent.hard  # never harder
    assert "readiness" in {c.code for c in result.changes}
    assert errors(result.plan, gated) == []
    again = adapt_plan(result.plan, gated, history, NOW)
    assert again.plan == result.plan
    tsb = [WellnessDay(NOW - dt.timedelta(1), ctl=40, atl=70), WellnessDay(NOW, ctl=40, atl=72)]
    assert "TSB" in (low_readiness(tsb, NOW) or "")
    assert low_readiness([WellnessDay(NOW, ctl=40, atl=72)], NOW) is None  # one day only


def test_strength_sessions_in_history() -> None:
    profile = with_strength(make_profile())
    plan = generate_plan(profile, TODAY)
    planned = [s for w in plan.weeks for s in w.strength if s.date < NOW]
    done_day, missed_day = planned[0].date, planned[1].date
    history = history_from(plan, NOW)
    gym = DayObservation(
        done_day, None, 1800, 17.5, activity_id="gym", activity_type="WeightTraining"
    )
    history = history.model_copy(update={"observations": [*history.observations, gym]})
    result = adapt_plan(plan, profile, history, NOW)
    strength = {s.date: s for w in result.plan.weeks for s in w.strength}
    assert strength[done_day].actual and missed_day not in strength
    assert errors(result.plan, profile) == []
    assert adapt_plan(result.plan, profile, history, NOW).plan == result.plan


def test_missed_key_this_week_moves_or_is_dropped(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    week = plan.weeks[2]
    key = next(s for s in week.sessions if s.intent.hard)
    today = key.date + dt.timedelta(days=1)
    history = history_from(plan, today, lambda s: 0.0 if s.date == key.date else 1)
    result = adapt_plan(plan, profile, history, today)
    codes = {c.date: c.code for c in result.changes}
    assert codes[key.date] == "missed_key"
    same = [
        s
        for s in result.plan.weeks[2].sessions
        if s.template_id == key.template_id and not s.actual
    ]
    assert len(same) <= 1  # moved once at most, never stacked
    if same:
        assert codes[same[0].date] == "missed_key_moved"
    assert errors(result.plan, profile) == []


def test_adjust_plan_and_push_leave_done_days_out(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    adapted = adjust_plan(plan, profile, history_from(plan, NOW).observations, NOW)
    assert any(s.actual for w in adapted.weeks for s in w.sessions)
    events = plan_events(adapted, plan.weeks[0].start)
    assert all(not e["name"].startswith("Done:") for e in events)
    assert '"source": "actual"' in dump_plan(adapted)
    assert '"source"' not in dump_plan(plan) and '"planned_sessions"' not in dump_plan(plan)


# ---------------------------------------------------------------------- property
def random_history(plan: Plan, today: dt.date, rng: random.Random) -> History:
    observations = []
    for week in plan.weeks:
        for session in week.sessions:
            if session.date >= today:
                continue
            roll = rng.random()
            factor = 0.0 if roll < 0.15 else rng.choice([0.5, 0.8, 1.0, 1.0, 1.2, 1.5])
            if factor:
                observations.append(
                    DayObservation(
                        session.date,
                        session.sport,
                        int(session.duration_s * factor),
                        round(session.tss * factor * rng.uniform(0.8, 1.3), 1),
                        activity_id=f"{session.date}-{session.sport.value}",
                        activity_type="Run" if session.sport is Sport.RUN else "Ride",
                    )
                )
            if rng.random() < 0.1:  # an extra activity
                sport = rng.choice(list(Sport))
                observations.append(
                    DayObservation(
                        session.date + dt.timedelta(days=1),
                        sport,
                        3600,
                        60.0,
                        activity_id=f"extra-{session.date}",
                        activity_type="Run" if sport is Sport.RUN else "Ride",
                    )
                )
    return History(synced=today, observations=[o for o in observations if o.date < today])


@pytest.mark.parametrize("seed", range(150))
def test_adapted_plans_pass_validation(seed: int) -> None:
    profile = random_profile(seed)
    plan = generate_plan(profile, TODAY)
    rng = random.Random(60_000 + seed)
    today = TODAY + dt.timedelta(days=rng.choice([3, 9, 16, 23, 30]))
    history = random_history(plan, today, rng)
    result = adapt_plan(plan, profile, history, today)
    assert errors(result.plan, profile) == []
    again = adapt_plan(result.plan, profile, history, today)
    assert again.plan == result.plan  # idempotent


@pytest.mark.parametrize("seed", range(0, 150, 5))
def test_adapted_plans_with_strength_and_amendments(seed: int) -> None:
    """Strength on, then a replan and random amendments of the adapted plan: still valid."""
    profile = random_strength(random_profile(seed), seed)
    plan = generate_plan(profile, TODAY)
    rng = random.Random(80_000 + seed)
    today = TODAY + dt.timedelta(days=rng.choice([3, 9, 16, 23]))
    history = random_history(plan, today, rng)
    adapted = adapt_plan(plan, profile, history, today).plan
    assert errors(adapted, profile) == []
    assert adapt_plan(adapted, profile, history, today).plan == adapted
    assert replan(adapted, profile, today).weeks == adapted.weeks  # replans see the same past
    reported: set[tuple[str, int | None]] = set()
    for _ in range(3):
        try:
            result = random_amendment(rng, adapted, profile, today)
        except AmendError:
            continue
        forced = result.amendment is not None and result.amendment.force
        if result.errors and not forced:
            continue
        reported |= {(issue.code, issue.week) for issue in result.errors}
        adapted = result.plan
    found = {
        (i.code, i.week) for i in validate_plan(adapted, profile) if i.severity is Severity.ERROR
    }
    assert found <= reported


# ---------------------------------------------------------------------- tampering
def scaled(week: Week, factor: float) -> Week:
    """The week with every planned step longer (completed activities unchanged)."""
    sessions = []
    for session in week.sessions:
        if not session.actual:
            steps = [
                step.model_copy(update={"duration_s": int(step.duration_s * factor)})
                for step in session.flat_steps()
            ]
            session = session.model_copy(update={"steps": steps})
        sessions.append(session)
    return week.model_copy(update={"sessions": sessions})


def test_tampering_catch_up_after_under_training(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    second, monday = plan.weeks[1], plan.weeks[2].start
    history = history_from(plan, monday, lambda s: 0.0 if second.start <= s.date < monday else 1)
    result = adapt_plan(plan, profile, history, monday)
    assert errors(result.plan, profile) == []
    weeks = list(result.plan.weeks)
    weeks[2] = scaled(weeks[2], 1.4)  # the week right after the missed one catches up
    tampered = result.plan.model_copy(update={"weeks": weeks})
    codes = {i.code for i in validate_plan(tampered, profile) if i.severity is Severity.ERROR}
    assert "under_training_catch_up" in codes


def test_tampering_overreach_raises_no_cap(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    result = adapt_plan(plan, profile, history_from(plan, NOW, lambda s: 1.5), NOW)
    impact = run_impact_multiplier(profile)
    refs = [ref_load(w, impact) for w in result.plan.weeks[:3]]
    raw = [week_weighted_load(w, impact) for w in result.plan.weeks[:3]]
    fourth = result.plan.weeks[3]
    # Between the guarded cap and the cap the raw (oversized) weeks would give.
    target = (load_cap(refs, profile) + load_cap(raw, profile)) / 2
    weeks = list(result.plan.weeks)
    weeks[3] = scaled(fourth, target / week_weighted_load(fourth, impact))
    tampered = result.plan.model_copy(update={"weeks": weeks})
    codes = {i.code for i in validate_plan(tampered, profile) if i.severity is Severity.ERROR}
    assert "ramp_rate" in codes


def test_done_days_are_not_checked_as_plans(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    history = history_from(plan, NOW, lambda s: 3.0)  # far over budget and caps
    result = adapt_plan(plan, profile, history, NOW)
    assert errors(result.plan, profile) == []


# ---------------------------------------------------------------------- calendar events
def test_calendar_events_are_guarded() -> None:
    records = [
        {
            "category": "SICK",
            "start_date_local": "2026-12-01T00:00:00",
            "end_date_local": "2026-12-03T00:00:00",
        },
        {"category": "holiday", "start_date_local": "2026-12-20"},
        {"category": "NOTE", "start_date_local": "2026-12-05"},
        {"category": "INJURED"},
        {"category": "RACE_A", "start_date_local": "2026-12-06"},
    ]
    events = calendar_events(records)
    assert [(e.category, e.start, e.end) for e in events] == [
        ("SICK", dt.date(2026, 12, 1), dt.date(2026, 12, 3)),
        ("HOLIDAY", dt.date(2026, 12, 20), dt.date(2026, 12, 20)),
    ]


def test_calendar_events_become_days_off(setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    fake = FakeIntervals()
    sick = {
        "category": "SICK",
        "start_date_local": "2026-12-08T00:00:00",
        "end_date_local": "2026-12-12T00:00:00",
        "external_id": "",
        "id": 1,
    }
    fake.events[1] = sick
    events = services.calendar_events(fake.client(), plan, NOW)
    assert len(events) == 1
    ignored = adapt_plan(plan, profile, history_from(plan, NOW), NOW, events=events)
    assert "calendar_event" not in {c.code for c in ignored.changes}  # option off
    data = profile.model_dump(mode="json", by_alias=True)
    data["preferences"]["adaptation"] = {"read_calendar_events": True}
    reading = Profile.model_validate(data)
    result = adapt_plan(plan, reading, history_from(plan, NOW), NOW, events=events)
    sick_days = {dt.date(2026, 12, 8) + dt.timedelta(days=i) for i in range(5)}
    assert not [s for w in result.plan.weeks for s in w.sessions if s.date in sick_days]
    assert "calendar_event" in {c.code for c in result.changes}
    assert errors(result.plan, reading) == []


# ---------------------------------------------------------------------- services, CLI, API
def test_cli_plan_adapt(tmp_path: Path, setup: tuple[Plan, Profile]) -> None:
    plan, profile = setup
    paths = {name: tmp_path / name for name in ("plan.json", "history.json", "profile.yaml")}
    paths["plan.json"].write_text(dump_plan(plan))
    paths["history.json"].write_text(dump_history(history_from(plan, NOW, lambda s: 0.5)))
    paths["profile.yaml"].write_text(EXAMPLE.read_text(encoding="utf-8"))
    args = [
        "plan",
        "adapt",
        str(paths["plan.json"]),
        "--history",
        str(paths["history.json"]),
        "--profile",
        str(paths["profile.yaml"]),
        "--today",
        NOW.isoformat(),
    ]
    runner = CliRunner()
    dry = runner.invoke(cli.app, [*args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "actual" in dry.output and "dry run" in dry.output
    assert paths["plan.json"].read_text() == dump_plan(plan)
    out = tmp_path / "plan.adapted.json"
    done = runner.invoke(cli.app, [*args, "-o", str(out)])
    assert done.exit_code == 0 and out.exists()
    assert '"source": "actual"' in out.read_text()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, Path, dict[str, dt.date]]]:
    data = tmp_path / "data"
    data.mkdir()
    (data / "profile.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    clock = {"today": TODAY}
    app = api.create_app(data, token=TOKEN, today=lambda: clock["today"], env={}, web_dir=None)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, data, clock


def test_api_plan_adapt(client: tuple[TestClient, Path, dict[str, dt.date]]) -> None:
    http, data, clock = client
    assert http.post("/plan/adapt", json={"expected_revision": 0}).status_code == 401
    assert http.post("/plan/generate", json={"expected_revision": 0}, headers=AUTH).is_success
    plan = services.PlannerService(services_storage(data)).plan()
    (data / "history.json").write_text(dump_history(history_from(plan, NOW)))
    clock["today"] = NOW
    dry = http.post("/plan/adapt", json={"expected_revision": 0, "dry_run": True}, headers=AUTH)
    body = dry.json()
    assert dry.status_code == 200 and body["dry_run"] and body["changed"]
    assert {c["code"] for c in body["changes"]} == {"actual"}
    assert body["changes"][0]["after"]["source"] == "actual"
    done = http.post("/plan/adapt", json={"expected_revision": 0}, headers=AUTH).json()
    assert done["revision"] == 1
    again = http.post("/plan/adapt", json={"expected_revision": 1}, headers=AUTH).json()
    assert (again["changed"], again["revision"]) == (False, 1)  # idempotent
    stale = http.post("/plan/adapt", json={"expected_revision": 0}, headers=AUTH)
    assert stale.status_code == 409
    week = http.get("/plan/weeks/1", headers=AUTH).json()
    assert {s["source"] for s in week["sessions"]} == {"actual"}


def services_storage(data: Path) -> "services.Storage":
    from endurance_planner.storage import FileStorage

    return FileStorage(data)


def test_web_app_offers_adapt() -> None:
    web = Path(api.__file__).parent / "web"
    script = (web / "app.js").read_text(encoding="utf-8")
    assert '"/plan/adapt"' in script and 'source === "actual"' in script


def test_profile_adaptation_defaults() -> None:
    profile = make_profile()
    assert not profile.preferences.adaptation.use_wellness
    assert not profile.preferences.adaptation.read_calendar_events
    text = yaml.safe_dump(profile.model_dump(mode="json", by_alias=True, exclude_defaults=True))
    assert "adaptation" not in text
