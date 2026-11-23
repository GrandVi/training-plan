"""Amendments of strength sessions (F10): shorten, skip, move, equipment, rung hold, replans."""

import datetime as dt
import random

import pytest

from builders import TODAY, with_exceptions, with_strength
from endurance_planner.amend import (
    AmendError,
    AmendResult,
    NoSession,
    SessionEdit,
    StrengthEdit,
    amend_session,
    amend_strength,
    amendment_lines,
    undo_amendment,
)
from endurance_planner.export.json_io import dump_plan
from endurance_planner.models.enums import AmendmentKind, Equipment, Scope, Severity
from endurance_planner.models.plan import Plan, StrengthSession
from endurance_planner.models.profile import Profile
from endurance_planner.planner import generate_plan
from endurance_planner.replan import change_lines, replan
from endurance_planner.serialization import parse_plan
from endurance_planner.strength import EXERCISES
from endurance_planner.validation import validate_plan
from test_property import random_amendment, random_profile
from test_strength_plan import random_strength, winter_profile


@pytest.fixture(scope="module")
def winter() -> tuple[Plan, Profile]:
    profile = winter_profile()
    return generate_plan(profile, TODAY), profile


def errors(plan: Plan, profile: Profile) -> list[str]:
    return [i.code for i in validate_plan(plan, profile) if i.severity is Severity.ERROR]


def strength_on(plan: Plan, date: dt.date) -> StrengthSession | None:
    return next((s for w in plan.weeks for s in w.strength if s.date == date), None)


def first_strength(plan: Plan, week: int = 1) -> StrengthSession:
    return plan.weeks[week].strength[0]


def test_shorten_drops_accessories_first_and_holds_the_rung(
    winter: tuple[Plan, Profile],
) -> None:
    plan, profile = winter
    session = first_strength(plan)
    assert any(e.accessory for e in session.exercises)
    result = amend_strength(plan, profile, TODAY, session.date, StrengthEdit(minutes=20))
    assert result.errors == []
    short = strength_on(result.plan, session.date)
    assert short is not None and short.pinned and short.amendment_id == "a1"
    assert short.minutes <= 20 and not any(e.accessory for e in short.exercises)
    assert [e.id for e in short.exercises] == [e.id for e in session.exercises if not e.accessory]
    assert short.rung is None  # under 80% of 30 min: the rung is held
    next_week = result.plan.weeks[2].strength[0]
    assert next_week.rung == session.rung  # repeated, not advanced
    assert plan.weeks[2].strength[0].rung == (session.rung or 0) + 1
    codes = {c.code for c in result.changes}
    assert {"amended", "rebalanced"} <= codes
    assert errors(result.plan, profile) == []


def test_small_cut_keeps_the_rung(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = plan.weeks[1].strength[1]  # B session, 35 min
    result = amend_strength(plan, profile, TODAY, session.date, StrengthEdit(minutes=30))
    short = strength_on(result.plan, session.date)
    assert short is not None and short.rung == session.rung
    assert result.plan.weeks[2].strength == plan.weeks[2].strength


def test_skip_is_not_made_up(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = first_strength(plan)
    result = amend_strength(plan, profile, TODAY, session.date, StrengthEdit(skip=True))
    week = result.plan.weeks[1]
    assert strength_on(result.plan, session.date) is None
    assert [s.variant for s in week.strength] == ["B"]  # the other one is not changed
    assert week.strength[0] == plan.weeks[1].strength[1]
    assert result.plan.weeks[2].strength[0].rung == session.rung  # rung held
    assert errors(result.plan, profile) == []
    again = replan(result.plan, profile, TODAY)
    assert again.weeks == result.plan.weeks  # a replan keeps the skip


def test_move_within_the_week(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = first_strength(plan)
    day = session.date + dt.timedelta(days=1)
    result = amend_strength(plan, profile, TODAY, session.date, StrengthEdit(move_to=day))
    assert result.errors == [] and strength_on(result.plan, session.date) is None
    moved = strength_on(result.plan, day)
    assert moved is not None and moved.pinned and moved.rung == session.rung
    lines = change_lines(result.changes)
    assert any("moved" in line for line in lines) and any("amended" in line for line in lines)
    with pytest.raises(AmendError, match="not in the week"):
        amend_strength(
            plan, profile, TODAY, session.date, StrengthEdit(move_to=day + dt.timedelta(7))
        )
    with pytest.raises(AmendError, match="before today"):
        amend_strength(
            plan, profile, TODAY + dt.timedelta(days=12), session.date, StrengthEdit(minutes=25)
        )


def test_move_before_a_hard_session_is_refused_unless_forced(
    winter: tuple[Plan, Profile],
) -> None:
    plan, profile = winter
    week = plan.weeks[1]
    hard = next(s for s in week.sessions if s.intent.hard)
    eve = hard.date - dt.timedelta(days=1)
    session = first_strength(plan)
    edit = StrengthEdit(move_to=eve)
    result = amend_strength(plan, profile, TODAY, session.date, edit, scope=Scope.SESSION)
    assert "strength_placement" in {i.code for i in result.errors}
    forced = amend_strength(plan, profile, TODAY, session.date, edit, force=True)
    assert forced.amendment is not None and forced.amendment.force


def test_equipment_change(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = first_strength(plan)
    edit = StrengthEdit(equipment=())
    result = amend_strength(plan, profile, TODAY, session.date, edit)
    hotel = strength_on(result.plan, session.date)
    assert hotel is not None and hotel.equipment_used == [Equipment.BODYWEIGHT]
    assert all(EXERCISES[e.id].equipment == (Equipment.BODYWEIGHT,) for e in hotel.exercises)
    assert hotel.rung == session.rung
    assert result.amendment is not None and result.amendment.equipment == []
    assert errors(result.plan, profile) == []
    bands = StrengthEdit(equipment=(Equipment.BANDS,))
    banded = amend_strength(result.plan, profile, TODAY, session.date, bands)
    assert banded.amendment is not None and banded.amendment.replaces == "a1"
    used = strength_on(banded.plan, session.date)
    assert used is not None and Equipment.BANDS in used.equipment_used
    assert errors(banded.plan, profile) == []


def test_supersede_keeps_earlier_flags_and_undo_restores(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = first_strength(plan)
    first = amend_strength(plan, profile, TODAY, session.date, StrengthEdit(equipment=()))
    second = amend_strength(first.plan, profile, TODAY, session.date, StrengthEdit(minutes=20))
    both = strength_on(second.plan, session.date)
    assert both is not None and both.minutes <= 20
    assert both.equipment_used == [Equipment.BODYWEIGHT]
    assert [a.active for a in second.plan.amendments] == [False, True]
    lines = amendment_lines(second.plan)
    assert "strength with" in lines[0] and "superseded by a2" in lines[0]
    undone = undo_amendment(second.plan, profile, TODAY, "a2")
    assert undone.plan.weeks == first.plan.weeks
    clean = undo_amendment(undone.plan, profile, TODAY, "a1")
    assert clean.plan.weeks == plan.weeks


def test_refusals(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = first_strength(plan)
    with pytest.raises(NoSession):
        amend_strength(
            plan, profile, TODAY, session.date - dt.timedelta(days=1), StrengthEdit(skip=True)
        )
    with pytest.raises(AmendError, match="at least 20 min"):
        amend_strength(plan, profile, TODAY, session.date, StrengthEdit(minutes=10))
    with pytest.raises(AmendError, match="nothing to change"):
        amend_strength(plan, profile, TODAY, session.date, StrengthEdit())
    with pytest.raises(AmendError, match="cannot be combined"):
        amend_strength(plan, profile, TODAY, session.date, StrengthEdit(skip=True, minutes=30))


def test_pinned_strength_survives_replans(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = plan.weeks[3].strength[0]
    result = amend_strength(plan, profile, TODAY, session.date, StrengthEdit(minutes=20))
    pinned = strength_on(result.plan, session.date)
    edited = with_strength(profile, minutes=45)
    replanned = replan(result.plan, edited, TODAY + dt.timedelta(days=3))
    assert strength_on(replanned, session.date) == pinned
    assert errors(replanned, edited) == []
    trip = with_exceptions(
        profile, [{"from": session.date.isoformat(), "mode": "limited", "minutes": {"run": 30}}]
    )
    assert strength_on(replan(result.plan, trip, TODAY), session.date) == pinned


def test_endurance_amendment_moves_strength_off_a_new_eve(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    week = plan.weeks[1]
    easy = next(s for s in week.sessions if s.date == week.strength[0].date + dt.timedelta(1))
    edit = SessionEdit(minutes=easy.duration_s // 60 + 15)
    result = amend_session(plan, profile, TODAY, easy.date, edit)
    assert errors(result.plan, profile) == []


def test_strength_amendment_round_trips_through_json(winter: tuple[Plan, Profile]) -> None:
    plan, profile = winter
    session = first_strength(plan)
    result = amend_strength(plan, profile, TODAY, session.date, StrengthEdit(equipment=()))
    text = dump_plan(result.plan)
    assert parse_plan(text) == result.plan
    amendment = result.plan.amendments[0]
    assert amendment.kind is AmendmentKind.STRENGTH and amendment.original_strength == session
    plain = amend_session(
        plan, profile, TODAY, plan.weeks[1].sessions[1].date, SessionEdit(minutes=40)
    )
    assert '"original_strength"' not in dump_plan(plain.plan)  # older logs are unchanged
    assert '"equipment"' not in dump_plan(plain.plan).split('"amendments"')[1]


def random_strength_amendment(
    rng: random.Random, plan: Plan, profile: Profile, today: dt.date
) -> AmendResult:
    sessions = [s for w in plan.weeks for s in w.strength if s.date >= today]
    if not sessions or rng.random() < 0.4:
        return random_amendment(rng, plan, profile, today)
    session = rng.choice(sessions)
    edit = rng.choice(
        [
            StrengthEdit(minutes=rng.choice([20, 25, 30, 45])),
            StrengthEdit(skip=True),
            StrengthEdit(move_to=session.date + dt.timedelta(days=rng.choice([-2, -1, 1, 2]))),
            StrengthEdit(equipment=tuple(rng.sample(list(Equipment), rng.randint(0, 2)))),
        ]
    )
    scope = rng.choice([Scope.REST, Scope.WEEK, Scope.SESSION])
    if rng.random() < 0.15 and plan.amendments:
        return undo_amendment(plan, profile, today, rng.choice(plan.amendments).id)
    return amend_strength(
        plan, profile, today, session.date, edit, scope=scope, force=rng.random() < 0.2
    )


@pytest.mark.parametrize("seed", range(0, 150, 2))
def test_random_strength_amendments_keep_plans_valid(seed: int) -> None:
    profile = random_strength(random_profile(seed), seed)
    plan = generate_plan(profile, TODAY)
    rng = random.Random(50_000 + seed)
    today = TODAY + dt.timedelta(days=rng.choice([0, 0, 3, 10, 24]))
    reported: set[tuple[str, int | None]] = set()
    for _ in range(rng.randint(1, 5)):
        try:
            result = random_strength_amendment(rng, plan, profile, today)
        except AmendError:
            continue
        forced = result.amendment is not None and result.amendment.force
        if result.errors and not forced:
            continue
        reported |= {(issue.code, issue.week) for issue in result.errors}
        plan = result.plan
    found = {(i.code, i.week) for i in validate_plan(plan, profile) if i.severity is Severity.ERROR}
    assert found <= reported
