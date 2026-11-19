"""F2: calendar exceptions planned in advance (availability.exceptions)."""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from builders import (
    EXAMPLE,
    TODAY,
    example_data,
    free_long_day_profile,
    run_10k_profile,
    with_exceptions,
)
from endurance_planner.config import load_profile
from endurance_planner.export.markdown import render_markdown
from endurance_planner.load import week_weighted_load
from endurance_planner.models.enums import ActivityType, BikeActivity, Severity, Slot, Sport
from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile
from endurance_planner.planner import generate_plan
from endurance_planner.rules import BreakReturn, baseline_load, break_return, reference
from endurance_planner.validation import validate_plan

DAY = dt.timedelta(days=1)
SKI_WEEK = {"from": "2026-12-21", "to": "2026-12-27", "mode": "off", "note": "ski week"}


def _errors(plan: Plan, profile: Profile) -> list[str]:
    return [str(i) for i in validate_plan(plan, profile) if i.severity is Severity.ERROR]


def _skeleton(plan: Plan) -> list[tuple[Any, ...]]:
    return [(w.index, w.start, w.phase, w.recovery, w.goal, w.races) for w in plan.weeks]


def _check_calendar(plan: Plan, profile: Profile) -> None:
    """No session on an unavailable date or above that date's minutes."""
    for week in plan.weeks:
        for session in week.sessions:
            allowed = profile.availability.minutes(session.date, session.sport)
            assert session.duration_s <= allowed * 60, (session.date, session.name)


@pytest.fixture(scope="module")
def base_plan() -> Plan:
    return generate_plan(run_10k_profile(), TODAY)


# ---------------------------------------------------------------------- profile
def test_exceptions_are_read_with_from_and_to(tmp_path: Path) -> None:
    data = example_data()
    data["availability"]["exceptions"] = [
        {"from": dt.date(2027, 1, 4), "to": dt.date(2027, 1, 10), "mode": "off", "note": "ski"},
        {"from": dt.date(2027, 2, 16), "mode": "limited", "minutes": {"bike": 45, "run": 0}},
        {
            "from": dt.date(2027, 3, 2),
            "mode": "limited",
            "minutes": {"run": 0},
            "bike_activity": "mtb",
        },
    ]
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    availability = load_profile(path).availability
    off, trip, mtb = availability.exceptions
    assert (off.start, off.end, off.last) == (dt.date(2027, 1, 4), dt.date(2027, 1, 10), off.end)
    assert trip.end is None and trip.last == trip.start  # one day
    minutes = availability.minutes
    assert minutes(dt.date(2027, 1, 3), Sport.BIKE) == 120  # Sunday before: usual
    assert minutes(dt.date(2027, 1, 4), Sport.BIKE) == 0  # bounds are inclusive
    assert minutes(dt.date(2027, 1, 10), Sport.RUN) == 0
    assert minutes(dt.date(2027, 2, 16), Sport.BIKE) == 45  # Tuesday, usually 60
    assert minutes(dt.date(2027, 2, 16), Sport.RUN) == 0
    assert minutes(dt.date(2027, 2, 17), Sport.RUN) == 45
    assert minutes(dt.date(2027, 3, 2), Sport.BIKE) == 60  # not listed: usual minutes
    assert availability.bike_activity(dt.date(2027, 3, 2)) is BikeActivity.MTB
    assert availability.bike_activity(dt.date(2027, 3, 4)) is BikeActivity.TRAINER  # thursday
    assert availability.bike_activity(dt.date(2027, 3, 3)) is None
    assert off.label() == "2027-01-04 to 2027-01-10 off (ski)"
    assert trip.label() == "2027-02-16 limited, bike 45 min, run 0 min"


def test_exceptions_as_written_by_hand(tmp_path: Path) -> None:
    """Unquoted `off` (a boolean in YAML 1.1) and flow mappings, as in the documentation."""
    text = (EXAMPLE.read_text(encoding="utf-8")).replace(
        "  exceptions: []",
        "  exceptions:\n"
        '    - {from: 2027-01-04, to: 2027-01-10, mode: off, note: "ski week"}\n'
        "    - {from: 2027-02-16, mode: limited, minutes: {bike: 45, run: 0}}\n"
        "    - {from: 2027-03-02, mode: limited, minutes: {run: 0}, bike_activity: mtb}\n"
        "  # exceptions:",
        1,
    )
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    off, trip, mtb = load_profile(path).availability.exceptions
    assert (off.mode, off.note, trip.mode, mtb.bike_activity) == (
        "off",
        "ski week",
        "limited",
        BikeActivity.MTB,
    )


@pytest.mark.parametrize(
    ("exceptions", "expected"),
    [
        ([{"from": "2027-01-10", "to": "2027-01-04", "mode": "off"}], "is before 'from'"),
        (
            [
                {"from": "2027-01-04", "to": "2027-01-10", "mode": "off"},
                {"from": "2027-01-10", "mode": "limited", "minutes": {"bike": 30}},
            ],
            "exceptions overlap",
        ),
        ([{"from": "2027-01-04", "mode": "off", "minutes": {"bike": 30}}], "takes no minutes"),
        ([{"from": "2027-01-04", "mode": "limited"}], "needs minutes or bike_activity"),
        ([{"from": "2027-01-04", "mode": "limited", "minutes": {"swim": 30}}], "swim"),
        ([{"from": "2027-01-04", "mode": "limited", "minutes": {"bike": -5}}], "greater than"),
        ([{"from": "2027-01-04", "mode": "away"}], "'off' or 'limited'"),
        ([{"start": "2027-01-04", "mode": "off", "extra": 1}], "extra"),
        ([{"from": "2027-03-10", "to": "2027-03-15", "mode": "off"}], "inside the off range"),
    ],
)
def test_invalid_exceptions(exceptions: list[dict[str, Any]], expected: str) -> None:
    data = example_data()
    data["availability"]["exceptions"] = exceptions
    with pytest.raises(ValueError, match=expected):
        Profile.model_validate(data)


def test_a_limited_range_on_an_a_race_is_allowed() -> None:
    race = {"from": "2027-03-14", "mode": "limited", "minutes": {"bike": 0}}
    with_exceptions(run_10k_profile(), [race])  # only 'off' ranges are refused


# ---------------------------------------------------------------------- scenarios
def test_a_week_off(base_plan: Plan) -> None:
    profile = with_exceptions(run_10k_profile(), [SKI_WEEK])
    plan = generate_plan(profile, TODAY)
    assert _errors(plan, profile) == []
    assert _skeleton(plan) == _skeleton(base_plan)  # phases and goal dates do not move
    assert plan.weeks[:5] == base_plan.weeks[:5]  # weeks before the exception are untouched
    assert plan.weeks[5].sessions == []  # 2026-12-21 is week 6
    _check_calendar(plan, profile)
    # Return from break: 8 days without training (Mon 21 to Mon 28, a rest day).
    week_back = plan.weeks[6]
    back = break_return(profile.availability, week_back.start, plan.weeks[0].start)
    assert back == BreakReturn(cap=0.85, week=1, gap_days=8)
    loads = [week_weighted_load(week, 1.25) for week in plan.weeks]
    cap = 0.85 * reference(loads[:6], baseline_load(profile))
    assert loads[6] <= cap + 0.5
    assert loads[6] < week_weighted_load(base_plan.weeks[6], 1.25)
    assert any(i.code == "return_from_break" and i.week == 7 for i in plan.warnings)
    # The lost week is not compensated: no later week is above the plan without exception.
    for week, before in zip(plan.weeks[7:], base_plan.weeks[7:], strict=True):
        assert week_weighted_load(week, 1.25) <= week_weighted_load(before, 1.25) + 0.5


def test_two_weeks_off_cap_two_weeks() -> None:
    off = {"from": "2026-12-21", "to": "2027-01-03", "mode": "off"}
    profile = with_exceptions(run_10k_profile(), [off])
    plan = generate_plan(profile, TODAY)
    assert _errors(plan, profile) == []
    start = plan.weeks[0].start
    caps = [break_return(profile.availability, week.start, start) for week in plan.weeks[6:9]]
    assert [c.cap if c else None for c in caps] == [None, 0.70, 0.85]
    codes = [(i.code, i.week) for i in plan.warnings]
    assert ("return_from_break", 8) in codes and ("return_from_break", 9) in codes


def test_a_trip_day_with_45_min_and_no_run(base_plan: Plan) -> None:
    trip = dt.date(2026, 12, 15)  # Tuesday: a 56-min key run in the plan without exception
    before = next(s for s in base_plan.weeks[4].sessions if s.date == trip)
    assert before.sport is Sport.RUN and before.duration_s > 45 * 60
    exception = {"from": str(trip), "mode": "limited", "minutes": {"bike": 45, "run": 0}}
    profile = with_exceptions(run_10k_profile(), [exception])
    plan = generate_plan(profile, TODAY)
    assert _errors(plan, profile) == []
    _check_calendar(plan, profile)
    on_trip = [s for s in plan.weeks[4].sessions if s.date == trip]
    assert all(s.sport is Sport.BIKE and s.duration_s <= 45 * 60 for s in on_trip)
    # The key run is still in the week, on another day.
    assert any(s.template_id == before.template_id for s in plan.weeks[4].sessions)


def test_an_mtb_only_day(base_plan: Plan) -> None:
    saturday = dt.date(2026, 12, 19)  # usually the long run
    exception = {"from": str(saturday), "mode": "limited", "minutes": {"run": 0}}
    exception["bike_activity"] = "mtb"
    profile = with_exceptions(run_10k_profile(), [exception])
    plan = generate_plan(profile, TODAY)
    assert _errors(plan, profile) == []
    _check_calendar(plan, profile)
    week = plan.weeks[4]
    ride = next(s for s in week.sessions if s.date == saturday)
    assert (ride.sport, ride.activity_type, ride.indoor) == (Sport.BIKE, ActivityType.MTB, False)
    long_run = next(s for s in week.sessions if s.slot is Slot.LONG)
    assert long_run.sport is Sport.RUN and long_run.date == saturday + DAY  # moved to Sunday


def test_a_free_long_day() -> None:
    base_profile = free_long_day_profile()
    base = generate_plan(base_profile, TODAY)
    saturday = dt.date(2027, 2, 13)  # peak week 13: long ride capped by Saturday's 240 min
    long_before = next(s for s in base.weeks[12].sessions if s.slot is Slot.LONG)
    assert (long_before.date, long_before.duration_s) == (saturday, 240 * 60)
    exception = {"from": str(saturday), "mode": "limited", "minutes": {"bike": 300}}
    profile = with_exceptions(base_profile, [{**exception, "note": "free Saturday"}])
    plan = generate_plan(profile, TODAY)
    assert _errors(plan, profile) == []
    long_after = next(s for s in plan.weeks[12].sessions if s.slot is Slot.LONG)
    assert long_after.date == saturday
    # Longer, but the long-session ramp cap still applies (+10% over 240 min, in 5-min steps).
    assert long_after.duration_s == 260 * 60
    assert plan.weeks[:12] == base.weeks[:12]


def test_planning_errors_and_warnings() -> None:
    near_race = {"from": "2027-02-01", "to": "2027-02-02", "mode": "off", "note": "work"}
    profile = with_exceptions(run_10k_profile(), [near_race])
    plan = generate_plan(profile, TODAY)
    warnings = [i for i in plan.warnings if i.code == "exception_before_race"]
    assert len(warnings) == 1 and "'10k' (2027-02-07)" in warnings[0].message
    past = {"from": "2026-10-01", "to": "2026-11-15", "mode": "off"}  # before the plan start
    ignored = generate_plan(with_exceptions(run_10k_profile(), [past]), TODAY)
    assert ignored.weeks == generate_plan(run_10k_profile(), TODAY).weeks


def test_plan_show_marks_the_affected_weeks() -> None:
    profile = with_exceptions(run_10k_profile(), [SKI_WEEK])
    text = render_markdown(generate_plan(profile, TODAY), profile)
    rows = [line for line in text.splitlines() if line.startswith("| 6 ")]
    assert rows and "build \\*" in rows[0]
    assert "\\* calendar exception this week" in text
    assert "Calendar exception: 2026-12-21 to 2026-12-27 off (ski week)" in text
    assert "calendar exception" not in render_markdown(
        generate_plan(run_10k_profile(), TODAY), run_10k_profile()
    )
