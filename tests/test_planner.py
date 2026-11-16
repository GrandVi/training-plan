"""Scenario: 10k in 12 weeks, run primary, 4 days a week, bike as aerobic support."""

import datetime as dt

import pytest

from builders import TODAY, goal, make_profile, run_10k_profile
from endurance_planner.export.json_io import dump_plan
from endurance_planner.load import low_intensity_ratio, week_weighted_load
from endurance_planner.models.enums import Phase, Severity, Slot, Sport, TargetKind
from endurance_planner.models.plan import Plan
from endurance_planner.periodization import PlanningError
from endurance_planner.planner import generate_plan, retarget_plan
from endurance_planner.rules import baseline_load, week_run_s
from endurance_planner.validation import validate_plan

RACE = dt.date(2027, 2, 7)


@pytest.fixture(scope="module")
def plan() -> Plan:
    return generate_plan(run_10k_profile(), TODAY)


def test_shape(plan: Plan) -> None:
    assert plan.id == "victor-20261116"
    assert len(plan.weeks) == 12
    assert plan.weeks[0].start == dt.date(2026, 11, 16)
    assert plan.weeks[-1].phase is Phase.TAPER
    assert plan.weeks[-1].races == ["10k"]
    assert all(week.goal == "10k" for week in plan.weeks)
    assert all(session.date < RACE for week in plan.weeks for session in week.sessions)


def test_is_valid_and_deterministic(plan: Plan) -> None:
    issues = validate_plan(plan, run_10k_profile())
    assert [issue for issue in issues if issue.severity is Severity.ERROR] == []
    assert dump_plan(generate_plan(run_10k_profile(), TODAY)) == dump_plan(plan)


def test_availability_is_respected(plan: Plan) -> None:
    for week in plan.weeks:
        assert len(week.sessions) <= 4
        assert week.duration_s <= 6 * 3600
        assert sum(s.sport is Sport.RUN for s in week.sessions) <= 4
        assert all(s.date.weekday() not in (0, 4) for s in week.sessions)  # Mon and Fri are rest


def test_week_one_is_not_a_jump(plan: Plan) -> None:
    profile = run_10k_profile()
    first = plan.weeks[0]
    assert week_weighted_load(first, 1.25) <= baseline_load(profile)
    assert week_run_s(first) <= 3 * 3600
    assert max(s.duration_s for s in first.sessions if s.sport is Sport.RUN) <= 70 * 60
    assert first.duration_s <= 5 * 3600


def test_run_specificity_with_bike_support(plan: Plan) -> None:
    hard = [s for week in plan.weeks for s in week.sessions if s.intent.hard]
    assert all(s.template_id and s.template_id.startswith("run_") for s in hard)
    assert sum(s.sport is Sport.RUN for s in hard) >= 0.8 * len(hard)
    long_sessions = [s for week in plan.weeks for s in week.sessions if s.slot is Slot.LONG]
    assert all(s.sport is Sport.RUN for s in long_sessions)
    bike = [s for week in plan.weeks for s in week.sessions if s.sport is Sport.BIKE]
    assert bike, "bike sessions provide aerobic volume"
    assert low_intensity_ratio(bike) > 0.9


def test_hard_sessions(plan: Plan) -> None:
    for week in plan.weeks:
        hard = sorted(s.date for s in week.sessions if s.intent.hard)
        assert len(hard) <= 2
        assert all((b - a).days >= 2 for a, b in zip(hard, hard[1:], strict=False))
    build = [w for w in plan.weeks if w.phase is Phase.BUILD and not w.recovery]
    assert all(sum(s.intent.hard for s in week.sessions) == 2 for week in build)


def test_progression_ladder(plan: Plan) -> None:
    tempo = [
        s.name
        for week in plan.weeks
        if not week.recovery
        for s in week.sessions
        if s.template_id == "run_tempo"
    ]
    assert tempo[:3] == [
        "Cruise intervals 3x8 min",
        "Cruise intervals 3x10 min",
        "Cruise intervals 4x8 min",
    ]
    long_runs = [
        s.duration_s
        for week in plan.weeks
        if not week.recovery
        for s in week.sessions
        if s.slot is Slot.LONG
    ]
    assert long_runs[0] == 70 * 60
    assert max(long_runs) == 100 * 60  # ceiling for a 10k
    assert all(b <= a * 1.1 + 300 for a, b in zip(long_runs, long_runs[1:], strict=False))


def test_recovery_and_taper(plan: Plan) -> None:
    loads = [week_weighted_load(week, 1.25) for week in plan.weeks]
    for index, week in enumerate(plan.weeks):
        if week.recovery:
            assert 0.5 * loads[index - 1] <= loads[index] <= 0.8 * loads[index - 1]
    race_week = plan.weeks[-1]
    peak_s = max(week.duration_s for week in plan.weeks[-5:-1])
    assert 0.4 * peak_s <= race_week.duration_s <= 0.75 * peak_s
    openers = [s for s in race_week.sessions if s.template_id == "run_openers"]
    assert len(openers) == 1
    assert 2 <= (RACE - openers[0].date).days <= 4
    assert any(s.name == "Pre-race shakeout" for s in race_week.sessions)


def test_targets_are_concrete(plan: Plan) -> None:
    for week in plan.weeks:
        for session in week.sessions:
            assert session.rationale.endswith(".")
            for step in session.flat_steps():
                assert step.resolved is not None
                if step.target.kind is not TargetKind.RPE:
                    assert step.resolved.label()
    race_pace = next(
        s for week in plan.weeks for s in week.sessions if s.template_id == "run_race_pace"
    )
    rep = next(step for step in race_pace.flat_steps() if step.distance_m)
    assert rep.resolved is not None and rep.resolved.pace_s_per_km is not None
    slow, fast = rep.resolved.pace_s_per_km
    assert fast <= 246 <= slow  # 41:00 for 10 km is 4:06 /km


def test_intensity_distribution_is_reported_and_bounded(plan: Plan) -> None:
    for week in plan.weeks:
        assert low_intensity_ratio(week.sessions) >= 0.75


def test_retarget_changes_only_absolute_values(plan: Plan) -> None:
    faster = make_profile(
        goals=[goal("10k", RACE, "run_10k", target={"time": "41:00"})],
        edits={"thresholds.run.threshold_pace": "4:05", "thresholds.bike.ftp_w": 280},
    )
    updated, _ = retarget_plan(plan, faster, TODAY)
    before = [s for week in plan.weeks for s in week.sessions]
    after = [s for week in updated.weeks for s in week.sessions]
    assert [(s.date, s.name, s.slot) for s in after] == [(s.date, s.name, s.slot) for s in before]
    changed = 0
    for old, new in zip(before, after, strict=True):
        for old_step, new_step in zip(old.flat_steps(), new.flat_steps(), strict=True):
            assert new_step.target == old_step.target
            changed += new_step.resolved != old_step.resolved
    assert changed > 0
    tempo_old = next(s for s in before if s.template_id == "run_tempo")
    tempo_new = next(s for s in after if s.template_id == "run_tempo")
    assert tempo_new.duration_s == tempo_old.duration_s


def test_start_must_be_a_future_monday() -> None:
    with pytest.raises(PlanningError, match="must be a Monday"):
        generate_plan(run_10k_profile(), TODAY, start=dt.date(2026, 11, 18))
    with pytest.raises(PlanningError, match="must be a Monday"):
        generate_plan(run_10k_profile(), TODAY, start=dt.date(2026, 11, 9))


def test_budget_below_goal_needs_is_said_explicitly() -> None:
    profile = make_profile(
        goals=[goal("10k", RACE, "run_10k")],
        edits={"availability.weekly_hours_budget": 3, "availability.days_per_week": 3},
    )
    plan = generate_plan(profile, TODAY)
    codes = {issue.code for issue in plan.warnings}
    assert "budget_below_goal_needs" in codes
    assert all(week.duration_s <= 3 * 3600 for week in plan.weeks)


def test_bike_supports_when_run_volume_is_capped() -> None:
    """Little current running: key sessions move to the bike instead of breaking the run cap."""
    profile = make_profile(
        goals=[goal("10k", RACE, "run_10k")],
        edits={
            "current_fitness.weekly_hours": {"bike": 6, "run": 0.75},
            "current_fitness.longest_recent_session_min": {"bike": 180, "run": 40},
        },
    )
    plan = generate_plan(profile, TODAY)
    runs = [week_run_s(week) for week in plan.weeks]
    assert runs[0] <= 45 * 60
    converted = [
        s for week in plan.weeks for s in week.sessions if s.sport is Sport.BIKE and s.intent.hard
    ]
    assert converted and all(s.name.endswith("[from run]") for s in converted)
    assert all("Done on the bike" in s.rationale for s in converted)
