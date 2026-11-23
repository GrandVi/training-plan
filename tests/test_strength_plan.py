"""Strength sessions in generated plans (F10): placement, validation rules, load, replans."""

import datetime as dt
import random
from typing import Any

import pytest

from builders import TODAY, goal, make_profile, with_exceptions, with_strength
from endurance_planner.export.markdown import daily_tss, fitness_csv
from endurance_planner.load import STRENGTH_TSS_PER_HOUR, week_weighted_load
from endurance_planner.models.enums import Equipment, Phase, Priority, Severity
from endurance_planner.models.plan import Plan, StrengthSession, Week
from endurance_planner.models.profile import Profile
from endurance_planner.planner import generate_plan
from endurance_planner.replan import replan
from endurance_planner.strength import (
    A_RACE_FREE_DAYS,
    EXERCISES,
    SESSIONS_PER_PHASE,
    exercise_equipment,
    heavy,
)
from endurance_planner.validation import validate_plan
from test_property import random_exceptions, random_profile

ALL_EQUIPMENT = [item for item in Equipment if item is not Equipment.BODYWEIGHT]


def errors(plan: Plan, profile: Profile) -> list[str]:
    return [i.code for i in validate_plan(plan, profile) if i.severity is Severity.ERROR]


def winter_profile(**settings: Any) -> Profile:
    """MTB marathon in early June: a 13-week base block through the winter."""
    profile = make_profile(goals=[goal("Spring marathon", dt.date(2027, 6, 6), "mtb_marathon")])
    return with_strength(profile, **settings)


def strength_sessions(plan: Plan) -> list[StrengthSession]:
    return [s for week in plan.weeks for s in week.strength]


def endurance(plan: Plan) -> dict[dt.date, Any]:
    return {s.date: s for week in plan.weeks for s in week.sessions}


def random_strength(profile: Profile, seed: int) -> Profile:
    """Random strength settings and equipment, from their own random stream."""
    rng = random.Random(30_000 + seed)
    return with_strength(
        profile,
        sessions_per_week=rng.randint(0, 3),
        minutes=rng.choice([20, 30, 40, 45, 60]),
        allow_extra_days=rng.random() < 0.3,
        equipment=["bodyweight", *rng.sample([e.value for e in ALL_EQUIPMENT], rng.randint(0, 3))],
    )


def check_strength(plan: Plan, profile: Profile) -> None:
    assert errors(plan, profile) == []
    availability = profile.availability
    budget = availability.weekly_hours_budget * 3600
    for week in plan.weeks:
        cap = min(SESSIONS_PER_PHASE[week.phase], availability.strength.sessions_per_week)
        assert len(week.strength) <= cap
        assert week.duration_s + week.strength_minutes * 60 <= budget + 1
        for session in week.strength:
            assert session.minutes <= availability.strength.minutes
            available = availability.equipment(session.date)
            for exercise in session.exercises:
                assert exercise_equipment(EXERCISES[exercise.id], available) is not None


@pytest.mark.parametrize("seed", range(150))
def test_random_strength_settings_give_valid_plans(seed: int) -> None:
    profile = random_strength(random_profile(seed), seed)
    plan = generate_plan(profile, TODAY)
    check_strength(plan, profile)
    if profile.availability.strength.sessions_per_week == 0:
        assert strength_sessions(plan) == []
        assert plan == generate_plan(random_profile(seed), TODAY)


@pytest.mark.parametrize("seed", range(0, 150, 3))
def test_random_strength_with_exceptions_and_equipment(seed: int) -> None:
    rng = random.Random(40_000 + seed)
    base = random_profile(seed)
    exceptions = random_exceptions(base, seed)
    for exception in exceptions:
        if exception["mode"] == "limited" and rng.random() < 0.5:
            exception["equipment"] = ["bodyweight"] if rng.random() < 0.5 else ["bands"]
    profile = random_strength(with_exceptions(base, exceptions), seed)
    check_strength(generate_plan(profile, TODAY), profile)


def test_winter_base_block_gives_two_sessions_a_week() -> None:
    profile = winter_profile()
    plan = generate_plan(profile, TODAY)
    base = [week for week in plan.weeks if week.phase is Phase.BASE]
    assert len(base) >= 12
    assert all(len(week.strength) == 2 for week in base[:12])
    sessions = endurance(plan)
    race = plan.goals[0].date
    for strength in strength_sessions(plan):
        same, after = sessions.get(strength.date), sessions.get(strength.date + dt.timedelta(1))
        assert same is None or not heavy(same)
        assert after is None or not heavy(after)
        assert not 0 <= (race - strength.date).days <= A_RACE_FREE_DAYS
        assert strength.template_id == "strength_base" or strength.phase is not Phase.BASE
    first = base[0].strength[0]
    assert {e.id for e in first.exercises} >= {"goblet_squat", "romanian_deadlift"}
    assert any(i.code == "strength_dropped" for i in plan.warnings)  # build: 2 keys + long
    check_strength(plan, profile)


def test_ladders_progress_in_loading_weeks_only() -> None:
    plan = generate_plan(winter_profile(), TODAY)
    rungs = [(week.recovery, week.strength[0].rung) for week in plan.weeks[:9]]
    levels = [rung or 0 for _, rung in rungs]
    assert levels[:4] == [0, 1, 2, 2]  # week 4 is a recovery week: same rung, fewer sets
    assert rungs[3][0] and "recovery" in plan.weeks[3].strength[0].name
    assert levels[4] == 3 and max(levels) == 3  # top of the ladder: it holds


def test_travel_exception_gives_bodyweight_sessions() -> None:
    plan = generate_plan(winter_profile(), TODAY)
    trip = plan.weeks[1]
    profile = with_exceptions(
        winter_profile(),
        [
            {
                "from": trip.start.isoformat(),
                "to": (trip.start + dt.timedelta(6)).isoformat(),
                "mode": "limited",
                "minutes": {"run": 45},
                "equipment": ["bodyweight"],
                "note": "trip",
            }
        ],
    )
    plan = generate_plan(profile, TODAY)
    check_strength(plan, profile)
    week = plan.weeks[1]
    assert week.strength
    for session in week.strength:
        assert session.equipment_used == [Equipment.BODYWEIGHT]
        assert {e.id for e in session.exercises} & {
            "bodyweight_squat",
            "split_squat",
            "walking_lunge",
            "glute_bridge",
        }
    assert plan.weeks[2].strength[0].equipment_used != [Equipment.BODYWEIGHT]


def test_injury_notes_leave_out_higher_impact_exercises() -> None:
    data = winter_profile(equipment=["bodyweight"]).model_dump(mode="json", by_alias=True)
    data["athlete"]["injury_notes"] = "sore knee"
    profile = Profile.model_validate(data)
    plan = generate_plan(profile, TODAY)
    ids = {e.id for s in strength_sessions(plan) for e in s.exercises}
    assert "split_squat" in ids and not any(EXERCISES[i].high_impact for i in ids)
    check_strength(plan, profile)


def test_no_strength_close_to_an_a_race() -> None:
    profile = with_strength(make_profile())
    plan = generate_plan(profile, TODAY)
    a_races = [g.date for g in plan.goals if g.priority is Priority.A]
    for strength in strength_sessions(plan):
        for race in a_races:
            assert not 0 <= (race - strength.date).days <= A_RACE_FREE_DAYS
    check_strength(plan, profile)


def test_strength_load_counts_in_the_projection_not_in_the_caps() -> None:
    profile = winter_profile()
    plan = generate_plan(profile, TODAY)
    week = plan.weeks[0]
    strength = week.strength[0]
    days = dict(daily_tss(plan))
    same_day = sum(s.tss for s in week.sessions if s.date == strength.date)
    assert days[strength.date] == pytest.approx(same_day + strength.minutes / 60 * 35, abs=0.1)
    assert STRENGTH_TSS_PER_HOUR == 35
    assert week_weighted_load(week, 1.0) == pytest.approx(week.tss)
    assert fitness_csv(plan, profile).count("\n") == len(plan.weeks) * 7 + 1


def test_zero_sessions_changes_nothing() -> None:
    plain = make_profile()
    assert generate_plan(with_strength(plain, sessions_per_week=0), TODAY) == generate_plan(
        plain, TODAY
    )


def test_replan_keeps_an_unchanged_plan_with_strength() -> None:
    profile = winter_profile()
    plan = generate_plan(profile, TODAY)
    for days in (0, 9, 17):
        assert replan(plan, profile, TODAY + dt.timedelta(days)).weeks == plan.weeks


def test_replan_after_profile_edit_rebuilds_strength() -> None:
    profile = winter_profile()
    plan = generate_plan(profile, TODAY)
    today = TODAY + dt.timedelta(days=9)
    fewer = with_strength(profile, sessions_per_week=1)
    replanned = replan(plan, fewer, today)
    issues = validate_plan(replanned, fewer)
    # Week 1 is over: its two sessions were done and stay (history is not rewritten).
    assert {(i.code, i.week) for i in issues if i.severity is Severity.ERROR} == {
        ("strength_count", 1)
    }
    for old, new in zip(plan.weeks, replanned.weeks, strict=True):
        past = [s for s in old.strength if s.date < today]
        assert [s for s in new.strength if s.date < today] == past
        if old.start > today:
            assert len(new.strength) <= 1
    off = replan(plan, with_strength(profile, sessions_per_week=0), today)
    assert all(s.date < today for s in strength_sessions(off))


# --- tampering: every rule has a check ---------------------------------------------------
def tampered(plan: Plan, index: int, **changes: Any) -> Plan:
    """The plan with the first strength session of week `index` changed."""
    week = plan.weeks[index]
    session = week.strength[0].model_copy(update=changes)
    weeks = list(plan.weeks)
    weeks[index] = week.model_copy(update={"strength": [session, *week.strength[1:]]})
    return plan.model_copy(update={"weeks": weeks})


def with_week(plan: Plan, index: int, week: Week) -> Plan:
    weeks = list(plan.weeks)
    weeks[index] = week
    return plan.model_copy(update={"weeks": weeks})


@pytest.fixture(scope="module")
def winter() -> tuple[Plan, Profile]:
    profile = winter_profile()
    return generate_plan(profile, TODAY), profile


def test_tampering_strength_on_a_hard_day(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    hard = next(s for s in plan.weeks[0].sessions if s.intent.hard)
    assert "strength_placement" in errors(tampered(plan, 0, date=hard.date), profile)


def test_tampering_strength_the_day_before_a_long_session(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    long = next(s for s in plan.weeks[0].sessions if heavy(s) and not s.intent.hard)
    eve = long.date - dt.timedelta(days=1)
    assert "strength_placement" in errors(tampered(plan, 0, date=eve), profile)


def test_tampering_strength_on_a_rest_day(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    monday = plan.weeks[2].start  # a rest day of the example athlete (and before a key day)

    def messages(checked: Profile) -> str:
        moved = tampered(plan, 2, date=monday)
        return " ".join(i.message for i in validate_plan(moved, checked) if i.code.startswith("s"))

    assert "a rest day" in messages(profile)
    assert "a rest day" not in messages(with_strength(profile, allow_extra_days=True))
    assert "hard or long session follows" in messages(profile)


def test_tampering_strength_sessions_too_close(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    week = plan.weeks[0]
    first, second = week.strength
    moved = second.model_copy(update={"date": first.date + dt.timedelta(days=1)})
    assert "strength_placement" in errors(
        with_week(plan, 0, week.model_copy(update={"strength": [first, moved]})), profile
    )


def test_tampering_strength_on_a_day_off(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    date = plan.weeks[3].strength[0].date
    off = with_exceptions(profile, [{"from": date.isoformat(), "mode": "off"}])
    assert "strength_placement" in errors(plan, off)


def test_tampering_too_many_training_days(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    week = plan.weeks[3]  # recovery week: strength on days without endurance
    assert any(s.date not in {x.date for x in week.sessions} for s in week.strength)
    fewer = make_profile(
        goals=[goal("Spring marathon", dt.date(2027, 6, 6), "mtb_marathon")],
        edits={"availability.days_per_week": len(week.sessions)},
    )
    assert "strength_placement" in errors(plan, with_strength(fewer))


def test_tampering_strength_phase(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    assert "strength_phase" in errors(tampered(plan, 0, template_id="strength_build"), profile)
    session = plan.weeks[0].strength[0]
    heavier = [e.model_copy(update={"reps": 3}) for e in session.exercises]
    assert "strength_phase" in errors(tampered(plan, 0, exercises=heavier), profile)
    assert "strength_phase" in errors(tampered(plan, 0, rung=9), profile)


def test_tampering_strength_equipment(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    bodyweight = with_strength(profile, equipment=["bodyweight"])
    assert "strength_equipment" in errors(plan, bodyweight)
    session = plan.weeks[0].strength[0]
    unknown = [session.exercises[0].model_copy(update={"id": "box_jump"})]
    assert "strength_equipment" in errors(tampered(plan, 0, exercises=unknown), profile)
    data = profile.model_dump(mode="json", by_alias=True)
    data["athlete"]["injury_notes"] = "knee"
    lunge = EXERCISES["walking_lunge"]
    exercise = session.exercises[0].model_copy(update={"id": lunge.id, "name": lunge.name})
    assert "strength_equipment" in errors(
        tampered(plan, 0, exercises=[exercise]), Profile.model_validate(data)
    )


def test_tampering_strength_near_a_race(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    race = plan.goals[0].date
    index = next(
        i for i, w in enumerate(plan.weeks) if w.start <= race <= w.start + dt.timedelta(6)
    )
    week = plan.weeks[index]
    session = (week.strength or plan.weeks[index - 1].strength)[0]
    moved = session.model_copy(update={"date": race - dt.timedelta(days=2)})
    others = [s for s in week.strength if s is not session]
    assert "strength_race_proximity" in errors(
        with_week(plan, index, week.model_copy(update={"strength": [*others, moved]})), profile
    )


def test_tampering_strength_count(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    assert "strength_count" in errors(plan, with_strength(profile, sessions_per_week=1))


def test_tampering_strength_budget(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    assert "weekly_budget" in errors(tampered(plan, 0, minutes=600), profile)
