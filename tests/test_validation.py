"""Each rule of validate_plan is triggered by tampering with a valid plan."""

import datetime as dt
from collections.abc import Callable

import pytest

from builders import TODAY, run_10k_profile, step, with_exceptions
from endurance_planner.models.enums import (
    ActivityType,
    Intent,
    Phase,
    Severity,
    Slot,
    Sport,
    TargetKind,
)
from endurance_planner.models.plan import Plan, Session
from endurance_planner.models.profile import Profile
from endurance_planner.planner import generate_plan
from endurance_planner.validation import PlanValidationError, assert_valid, validate_plan

DAY = dt.timedelta(days=1)


@pytest.fixture(scope="module")
def valid_plan() -> Plan:
    return generate_plan(run_10k_profile(), TODAY)


def _errors(plan: Plan, today: dt.date | None = None) -> set[str]:
    issues = validate_plan(plan, run_10k_profile(), today)
    return {issue.code for issue in issues if issue.severity is Severity.ERROR}


def _stretch(session: Session, factor: float) -> None:
    for item in session.flat_steps():
        item.duration_s = round(item.duration_s * factor)


def _easy(plan: Plan, week: int) -> Session:
    return next(s for s in plan.weeks[week].sessions if not s.intent.hard and s.slot is Slot.EASY)


def _key(plan: Plan, week: int) -> Session:
    return next(s for s in plan.weeks[week].sessions if s.intent.hard)


def ramp_rate(plan: Plan) -> None:
    for session in plan.weeks[2].sessions:
        _stretch(session, 1.5)


def run_volume_ramp(plan: Plan) -> None:
    _stretch(next(s for s in plan.weeks[1].sessions if s.slot is Slot.LONG), 1.5)


def long_session_ramp(plan: Plan) -> None:
    _stretch(next(s for s in plan.weeks[0].sessions if s.sport is Sport.BIKE), 1.3)


def weekly_budget(plan: Plan) -> None:
    for session in plan.weeks[5].sessions:
        _stretch(session, 2.0)


def day_limit(plan: Plan) -> None:
    session = _easy(plan, 0)
    session.date = plan.weeks[0].start  # Monday is a rest day


def training_days(plan: Plan) -> None:
    week = plan.weeks[0]
    extra = week.sessions[0].model_copy(deep=True)
    extra.date = week.start + 3 * DAY
    assert all(s.date != extra.date for s in week.sessions)
    week.sessions.append(extra)


def run_frequency(plan: Plan) -> None:
    week = plan.weeks[0]
    template = next(s for s in week.sessions if s.sport is Sport.RUN and s.slot is Slot.EASY)
    used = {s.date for s in week.sessions}
    for offset in range(7):
        date = week.start + offset * DAY
        if date not in used:
            week.sessions.append(template.model_copy(update={"date": date}, deep=True))


def session_on_race_day(plan: Plan) -> None:
    plan.weeks[-1].sessions[-1].date = plan.goals[0].date


def two_sessions_same_day(plan: Plan) -> None:
    week = plan.weeks[0]
    week.sessions[1].date = week.sessions[0].date


def session_outside_week(plan: Plan) -> None:
    plan.weeks[0].sessions[0].date += 14 * DAY


def weeks_not_contiguous(plan: Plan) -> None:
    plan.weeks[3].start += 7 * DAY


def missing_recovery_week(plan: Plan) -> None:
    for week in plan.weeks:
        week.recovery = False


def recovery_week_too_hard(plan: Plan) -> None:
    recovery = next(week for week in plan.weeks if week.recovery)
    for session in recovery.sessions:
        _stretch(session, 1.4)


def missing_taper(plan: Plan) -> None:
    plan.weeks[-1].phase = Phase.PEAK


def taper_volume(plan: Plan) -> None:
    for session in plan.weeks[-1].sessions:
        _stretch(session, 1.5)


def too_many_hard_sessions(plan: Plan) -> None:
    for session in plan.weeks[6].sessions:
        session.intent = Intent.THRESHOLD


def hard_session_spacing(plan: Plan) -> None:
    first, second = [s for s in plan.weeks[6].sessions if s.intent.hard]
    plan.weeks[6].sessions = [s for s in plan.weeks[6].sessions if s.date != first.date + DAY]
    second.date = first.date + DAY


def consecutive_high_impact_runs(plan: Plan) -> None:
    week = plan.weeks[1]
    long_run = next(s for s in week.sessions if s.slot is Slot.LONG)
    key = _key(plan, 1)
    week.sessions = [s for s in week.sessions if s.date != long_run.date - DAY or s is key]
    key.date = long_run.date - DAY


def intensity_distribution(plan: Plan) -> None:
    for session in plan.weeks[2].sessions:
        if not session.intent.hard:
            session.steps = [step(session.duration_s, 0.95, 1.0, TargetKind.PACE)]


def unresolved_target(plan: Plan) -> None:
    next(_key(plan, 0).flat_steps()).resolved = None


TAMPERINGS: list[Callable[[Plan], None]] = [
    ramp_rate,
    run_volume_ramp,
    long_session_ramp,
    weekly_budget,
    day_limit,
    training_days,
    run_frequency,
    session_on_race_day,
    two_sessions_same_day,
    session_outside_week,
    weeks_not_contiguous,
    missing_recovery_week,
    recovery_week_too_hard,
    missing_taper,
    taper_volume,
    too_many_hard_sessions,
    hard_session_spacing,
    consecutive_high_impact_runs,
    intensity_distribution,
    unresolved_target,
]


def test_generated_plan_has_no_error(valid_plan: Plan) -> None:
    assert _errors(valid_plan) == set()
    assert_valid(valid_plan, run_10k_profile())


@pytest.mark.parametrize("tamper", TAMPERINGS, ids=lambda f: f.__name__)
def test_rule_is_enforced(valid_plan: Plan, tamper: Callable[[Plan], None]) -> None:
    plan = valid_plan.model_copy(deep=True)
    tamper(plan)
    assert tamper.__name__ in _errors(plan)


def test_session_in_past(valid_plan: Plan) -> None:
    assert "session_in_past" in _errors(valid_plan, today=valid_plan.weeks[0].start + 3 * DAY)


def test_assert_valid_fails_loudly(valid_plan: Plan) -> None:
    plan = valid_plan.model_copy(deep=True)
    missing_taper(plan)
    with pytest.raises(PlanValidationError, match="no taper before '10k'") as error:
        assert_valid(plan, run_10k_profile())
    assert all(issue.week == 12 for issue in error.value.issues)


def test_cross_sport_spacing_is_a_warning_when_allowed(valid_plan: Plan) -> None:
    profile = run_10k_profile()
    profile.availability.swap_rules.no_hard_run_after_hard_bike = False
    plan = valid_plan.model_copy(deep=True)
    first, second = [s for s in plan.weeks[6].sessions if s.intent.hard]
    plan.weeks[6].sessions = [s for s in plan.weeks[6].sessions if s.date != first.date + DAY]
    second.date = first.date + DAY
    second.sport = Sport.BIKE
    issues = [i for i in validate_plan(plan, profile) if i.code == "hard_session_spacing"]
    assert [issue.severity for issue in issues] == [Severity.WARNING]


# ---------------------------------------------------------------------- calendar exceptions
def _exception_errors(plan: Plan, profile: Profile) -> set[str]:
    return {i.code for i in validate_plan(plan, profile) if i.severity is Severity.ERROR}


def test_session_on_unavailable_date() -> None:
    off = {"from": "2026-12-21", "to": "2026-12-27", "mode": "off"}
    trip = {"from": "2026-12-15", "mode": "limited", "minutes": {"bike": 45, "run": 0}}
    profile = with_exceptions(run_10k_profile(), [off, trip])
    plan = generate_plan(profile, TODAY)
    assert _exception_errors(plan, profile) == set()
    moved = plan.model_copy(deep=True)
    moved.weeks[5].sessions.append(moved.weeks[6].sessions[0].model_copy(deep=True))
    moved.weeks[5].sessions[-1].date = dt.date(2026, 12, 22)  # inside the week off
    assert "session_on_unavailable_date" in _exception_errors(moved, profile)
    too_long = plan.model_copy(deep=True)
    week = too_long.weeks[4]
    week.sessions = [s for s in week.sessions if s.date != dt.date(2026, 12, 15)]
    ride = next(s for w in too_long.weeks for s in w.sessions if s.sport is Sport.BIKE)
    week.sessions.append(ride.model_copy(update={"date": dt.date(2026, 12, 15)}, deep=True))
    assert ride.duration_s > 45 * 60
    assert "session_on_unavailable_date" in _exception_errors(too_long, profile)


def test_bike_activity_mismatch() -> None:
    mtb_day = {"from": "2026-12-19", "mode": "limited", "minutes": {"run": 0}}
    profile = with_exceptions(run_10k_profile(), [{**mtb_day, "bike_activity": "mtb"}])
    plan = generate_plan(profile, TODAY)
    assert _exception_errors(plan, profile) == set()
    ride = next(s for s in plan.weeks[4].sessions if s.date == dt.date(2026, 12, 19))
    ride.activity_type = ActivityType.RIDE
    assert "bike_activity_mismatch" in _exception_errors(plan, profile)


def test_return_from_break() -> None:
    off = {"from": "2026-12-21", "to": "2026-12-27", "mode": "off"}
    profile = with_exceptions(run_10k_profile(), [off])
    plan = generate_plan(profile, TODAY)
    assert _exception_errors(plan, profile) == set()
    for session in plan.weeks[6].sessions:  # first week back
        _stretch(session, 1.15)
    assert "return_from_break" in _exception_errors(plan, profile)
