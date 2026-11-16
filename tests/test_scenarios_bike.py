"""Scenarios: MTB marathon in 16 weeks, the full example profile, and a forced swap."""

import datetime as dt

import pytest

from builders import TODAY, goal, make_profile, mtb_marathon_profile
from endurance_planner.load import low_intensity_ratio, weighted_load
from endurance_planner.models.enums import ActivityType, Intent, Phase, Severity, Slot, Sport
from endurance_planner.models.plan import Plan
from endurance_planner.planner import generate_plan
from endurance_planner.swap import SwapError, swap_session
from endurance_planner.thresholds import ResolvedThresholds, resolve_thresholds
from endurance_planner.validation import validate_plan

IMPACT = 1.25


@pytest.fixture(scope="module")
def plan() -> Plan:
    return generate_plan(mtb_marathon_profile(), TODAY)


@pytest.fixture(scope="module")
def thresholds() -> ResolvedThresholds:
    return resolve_thresholds(mtb_marathon_profile(), TODAY)[0]


def _sessions(plan: Plan) -> list:  # type: ignore[type-arg]
    return [s for week in plan.weeks for s in week.sessions]


def test_mtb_marathon_shape(plan: Plan) -> None:
    assert len(plan.weeks) == 16
    assert [week.phase for week in plan.weeks[-2:]] == [Phase.TAPER, Phase.TAPER]
    assert plan.weeks[-1].races == ["MTB marathon"]
    errors = [
        i for i in validate_plan(plan, mtb_marathon_profile()) if i.severity is Severity.ERROR
    ]
    assert errors == []
    codes = {issue.code for issue in plan.warnings}
    assert "budget_below_goal_needs" in codes  # 8 h budget, about 9 h recommended
    assert "key_sessions_capped_by_day" in codes  # 60 min on tue / thu


def test_bike_specificity_with_one_run(plan: Plan) -> None:
    for week in plan.weeks:
        assert len(week.sessions) <= 5
        runs = [s for s in week.sessions if s.sport is Sport.RUN]
        assert len(runs) <= 1
        assert all(not s.intent.hard and s.slot is Slot.EASY for s in runs)
    loading = [w for w in plan.weeks if not w.recovery and w.phase in (Phase.BUILD, Phase.PEAK)]
    assert all(sum(s.sport is Sport.RUN for s in week.sessions) == 1 for week in loading)
    hard = [s for s in _sessions(plan) if s.intent.hard]
    assert hard and all(s.sport is Sport.BIKE for s in hard)
    used = {s.template_id for s in hard}
    assert {"bike_sweet_spot", "bike_threshold", "bike_vo2max", "bike_mtb_punches"} <= used
    assert "bike_over_unders" in used  # peak


def test_long_ride_progression_and_specific_blocks(plan: Plan) -> None:
    longs = [s for s in _sessions(plan) if s.slot is Slot.LONG]
    assert all(s.sport is Sport.BIKE and s.activity_type is ActivityType.MTB for s in longs)
    assert longs[0].duration_s == 180 * 60  # longest recent ride
    assert max(s.duration_s for s in longs) == 215 * 60  # about 80% of the 4.5 h race
    assert all("60-90 g of carbohydrate" in s.notes for s in longs)
    with_tempo = [s for s in longs if s.name == "Long ride with tempo blocks"]
    assert with_tempo
    assert all(low_intensity_ratio([s]) < 1 for s in with_tempo)
    weeks = {week.index: week for week in plan.weeks}
    for session in with_tempo:
        week = next(w for w in weeks.values() if session in w.sessions)
        assert week.phase in (Phase.BUILD, Phase.PEAK) and not week.recovery


def test_technical_sessions_are_flagged(plan: Plan) -> None:
    technical = [s for s in _sessions(plan) if s.technical]
    assert len(technical) >= 8
    for session in technical:
        assert session.intent is Intent.TECHNICAL
        assert session.activity_type is ActivityType.MTB
        assert not session.indoor and not session.intent.hard
    for week in plan.weeks:
        assert sum(s.technical for s in week.sessions) <= 1
    indoor = [s for s in _sessions(plan) if s.indoor]
    assert indoor and all(s.date.weekday() in (1, 3) for s in indoor)  # tue, thu
    assert all(s.activity_type is ActivityType.RIDE for s in indoor)


def test_example_profile_chains_both_goals() -> None:
    profile = make_profile()
    plan = generate_plan(profile, TODAY)
    assert len(plan.weeks) == 29
    goals = [week.goal for week in plan.weeks]
    assert goals == ["10k local race"] * 17 + ["MTB marathon"] * 12
    assert plan.weeks[16].races == ["10k local race"]
    assert plan.weeks[17].phase is Phase.TRANSITION
    assert all(not s.intent.hard for s in plan.weeks[17].sessions)
    assert plan.weeks[-1].races == ["MTB marathon"]
    first_block = [s for week in plan.weeks[:17] for s in week.sessions if s.slot is Slot.LONG]
    second_block = [s for week in plan.weeks[18:] for s in week.sessions if s.slot is Slot.LONG]
    assert all(s.sport is Sport.RUN for s in first_block)
    assert all(s.sport is Sport.BIKE for s in second_block)


def test_minor_race_gets_a_mini_taper() -> None:
    profile = make_profile(
        goals=[
            goal("MTB marathon", dt.date(2027, 3, 7), "mtb_marathon"),
            goal("XC tune-up", dt.date(2027, 1, 24), "mtb_xc", priority="B"),
        ],
    )
    with_race = generate_plan(profile, TODAY)
    without = generate_plan(mtb_marathon_profile(), TODAY)
    week = next(w for w in with_race.weeks if "XC tune-up" in w.races)
    same_week = without.weeks[week.index - 1]
    race_day = dt.date(2027, 1, 24)
    assert all(s.date != race_day for s in week.sessions)
    eve = [s for s in week.sessions if s.date == race_day - dt.timedelta(days=1)]
    assert all(not s.intent.hard and s.duration_s <= 40 * 60 for s in eve)
    assert week.duration_s < same_week.duration_s
    assert sum(s.intent.hard for s in week.sessions) < sum(
        s.intent.hard for s in same_week.sessions
    )


# ------------------------------------------------------------------ forced swap
def test_swap_bike_key_to_run_is_flagged_by_revalidation(
    plan: Plan, thresholds: ResolvedThresholds
) -> None:
    """Week with a forced swap: the conversion works, the week's rules are re-checked."""
    profile = mtb_marathon_profile()
    week = next(w for w in plan.weeks if w.phase is Phase.BUILD and not w.recovery)
    key = next(s for s in week.sessions if s.template_id == "bike_threshold")
    swapped, issues = swap_session(plan, key.date, profile, thresholds)
    new = next(s for w in swapped.weeks for s in w.sessions if s.date == key.date)
    assert (new.sport, new.activity_type) == (Sport.RUN, ActivityType.RUN)
    assert new.intent is key.intent and new.slot is key.slot
    assert new.name == f"{key.name} [from bike]"
    assert new.duration_s < key.duration_s
    assert new.duration_s <= 60 * 60  # thursday / tuesday run limit
    assert weighted_load(new, IMPACT) == pytest.approx(weighted_load(key, IMPACT), rel=0.25)
    assert all(step.resolved and step.resolved.pace_s_per_km for step in new.flat_steps())
    # The rest of the plan is untouched.
    assert [s for w in swapped.weeks for s in w.sessions if s.date != key.date] == [
        s for w in plan.weeks for s in w.sessions if s.date != key.date
    ]
    # A second run doubles the week's run volume: the run-volume ramp rule fires.
    codes = {(issue.code, issue.severity) for issue in issues}
    assert ("run_volume_ramp", Severity.ERROR) in codes
    assert all(issue.week in (week.index, week.index + 1) for issue in issues)


def test_swap_easy_ride_and_back(plan: Plan, thresholds: ResolvedThresholds) -> None:
    profile = mtb_marathon_profile()
    ride = next(
        s
        for s in plan.weeks[3].sessions
        if s.sport is Sport.BIKE and s.slot is Slot.EASY and not s.technical
    )
    to_run, issues = swap_session(plan, ride.date, profile, thresholds)
    run = next(s for s in to_run.weeks[3].sessions if s.date == ride.date)
    assert run.sport is Sport.RUN and run.duration_s < 0.6 * ride.duration_s
    assert "swap_shortened" not in {issue.code for issue in issues}
    assert weighted_load(run, IMPACT) == pytest.approx(weighted_load(ride, IMPACT), rel=0.05)
    back, _ = swap_session(to_run, ride.date, profile, thresholds)
    again = next(s for s in back.weeks[3].sessions if s.date == ride.date)
    assert again.name == ride.name
    assert again.duration_s == pytest.approx(ride.duration_s, rel=0.05)
    assert again.tss == pytest.approx(ride.tss, rel=0.05)


def test_swap_technical_session_warns(plan: Plan, thresholds: ResolvedThresholds) -> None:
    skills = next(s for s in _sessions(plan) if s.technical)
    swapped, issues = swap_session(plan, skills.date, mtb_marathon_profile(), thresholds)
    new = next(s for w in swapped.weeks for s in w.sessions if s.date == skills.date)
    assert new.sport is Sport.RUN and not new.technical
    assert "swap_intent_lost" in {issue.code for issue in issues}
    assert new.duration_s <= 90 * 60  # sunday run limit


def test_swap_is_shortened_to_fit_the_day(plan: Plan, thresholds: ResolvedThresholds) -> None:
    profile = make_profile(
        goals=[
            goal("MTB marathon", dt.date(2027, 3, 7), "mtb_marathon", target={"duration_h": 4.5})
        ],
        edits={
            "current_fitness.weekly_hours": {"bike": 7, "run": 0.75},
            "availability.weekdays.sat": {"bike": 240, "run": 60},
        },
    )
    long_ride = next(s for s in plan.weeks[0].sessions if s.slot is Slot.LONG)
    swapped, issues = swap_session(plan, long_ride.date, profile, thresholds)
    new = next(s for s in swapped.weeks[0].sessions if s.date == long_ride.date)
    assert new.duration_s <= 60 * 60
    assert "swap_shortened" in {issue.code for issue in issues}


def test_swap_refusals(plan: Plan, thresholds: ResolvedThresholds) -> None:
    profile = mtb_marathon_profile()
    with pytest.raises(SwapError, match="no session on"):
        swap_session(plan, plan.weeks[0].start, profile, thresholds)  # Monday: rest
    no_run = mtb_marathon_profile()
    no_run.availability.swap_rules.allow_run_instead_of_bike = False
    key = next(s for s in plan.weeks[0].sessions if s.intent.hard)
    with pytest.raises(SwapError, match="do not allow"):
        swap_session(plan, key.date, no_run, thresholds)
