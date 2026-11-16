"""Scenario: ultra-endurance event in 20 weeks (bike and run variants)."""

import datetime as dt

import pytest

from builders import TODAY, ultra_profile
from endurance_planner.load import low_intensity_ratio
from endurance_planner.models.enums import Phase, Severity, Slot, Sport
from endurance_planner.models.plan import Plan
from endurance_planner.planner import generate_plan
from endurance_planner.validation import validate_plan

DAY = dt.timedelta(days=1)


@pytest.fixture(scope="module", params=["bike", "run"])
def plan(request: pytest.FixtureRequest) -> Plan:
    return generate_plan(ultra_profile(request.param), TODAY)


def _sport(plan: Plan) -> Sport:
    assert plan.goals[0].sport is not None
    return plan.goals[0].sport


def test_shape_and_validity(plan: Plan) -> None:
    assert len(plan.weeks) == 20
    assert [week.phase for week in plan.weeks[-2:]] == [Phase.TAPER, Phase.TAPER]
    base = sum(week.phase is Phase.BASE for week in plan.weeks)
    assert base == 9  # half of the 18 pre-taper weeks
    issues = validate_plan(plan, ultra_profile(_sport(plan).value))
    assert [issue for issue in issues if issue.severity is Severity.ERROR] == []
    assert "low_intensity_share" not in {issue.code for issue in issues}


def test_durability_focus_without_vo2max(plan: Plan) -> None:
    sessions = [s for week in plan.weeks for s in week.sessions]
    used = {s.template_id for s in sessions if s.template_id}
    assert not any("vo2" in template or "800" in template for template in used)
    for week in plan.weeks:
        assert sum(s.intent.hard for s in week.sessions) <= 1
        assert low_intensity_ratio(week.sessions) >= 0.85


def test_back_to_back_long_days(plan: Plan) -> None:
    sport = _sport(plan)
    pairs = 0
    for week in plan.weeks:
        long_days = {s.slot: s for s in week.sessions if s.slot in (Slot.LONG, Slot.LONG_2)}
        if Slot.LONG_2 not in long_days:
            continue
        pairs += 1
        first, second = long_days[Slot.LONG], long_days[Slot.LONG_2]
        assert week.phase in (Phase.BUILD, Phase.PEAK) and not week.recovery
        assert second.date == first.date + DAY
        assert first.sport is sport and second.sport is sport
        assert second.duration_s < first.duration_s
        assert second.name.endswith("(day 2)")
        assert not first.intent.hard and not second.intent.hard
    loading = [w for w in plan.weeks if w.phase in (Phase.BUILD, Phase.PEAK) and not w.recovery]
    assert pairs == len(loading) >= 6


def test_long_sessions_grow_and_carry_fueling_notes(plan: Plan) -> None:
    sport = _sport(plan)
    longs = [s for week in plan.weeks for s in week.sessions if s.slot is Slot.LONG]
    assert all(s.sport is sport for s in longs)
    assert all("60-90 g of carbohydrate" in s.notes for s in longs)
    start, ceiling = {Sport.BIKE: (210, 360), Sport.RUN: (100, 180)}[sport]
    assert longs[0].duration_s == start * 60
    assert max(s.duration_s for s in longs) == ceiling * 60
    race_week_long = [s for s in plan.weeks[-1].sessions if s.slot is Slot.LONG]
    assert race_week_long == []
