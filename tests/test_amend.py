"""F3: amendments of a running plan (amend.py) and their effect on the rest of the plan."""

import datetime as dt

import pytest

from builders import TODAY, run_10k_profile, with_exceptions
from endurance_planner.amend import (
    AmendError,
    AmendResult,
    SessionEdit,
    add_unavailable,
    amend_session,
    amendment_lines,
    swap_sport,
    undo_amendment,
)
from endurance_planner.load import week_weighted_load
from endurance_planner.models.enums import (
    ActivityType,
    AmendmentKind,
    BikeActivity,
    Intent,
    Scope,
    Severity,
    Slot,
    Sport,
)
from endurance_planner.models.plan import Plan, Session, Week
from endurance_planner.planner import generate_plan
from endurance_planner.replan import replan
from endurance_planner.rules import week_run_s
from endurance_planner.validation import validate_plan

DAY = dt.timedelta(days=1)
PROFILE = run_10k_profile()
IMPACT = 1.25
# Week 6 (build): Tue 22 tempo key (rung 3 of 5, 60 min), Thu 24 VO2max key, Sat 26 long run,
# Sun 27 endurance ride. Week 9: Tue 12 tempo key, the next one after week 6.
TEMPO_6 = dt.date(2026, 12, 22)
VO2_6 = dt.date(2026, 12, 24)
LONG_6 = dt.date(2026, 12, 26)
RIDE_6 = dt.date(2026, 12, 27)
EASY_RUN_5 = dt.date(2026, 12, 16)
TEMPO_9 = dt.date(2027, 1, 12)


@pytest.fixture(scope="module")
def plan() -> Plan:
    return generate_plan(PROFILE, TODAY)


def _session(plan: Plan, date: dt.date) -> Session | None:
    return next((s for week in plan.weeks for s in week.sessions if s.date == date), None)


def _week(plan: Plan, date: dt.date) -> Week:
    return plan.weeks[(date - plan.weeks[0].start).days // 7]


def _errors(plan: Plan) -> list[str]:
    return [str(i) for i in validate_plan(plan, PROFILE) if i.severity is Severity.ERROR]


def _codes(result: AmendResult) -> dict[dt.date, str]:
    return {change.date: change.code for change in result.changes}


def _next_tempo(plan: Plan, after: dt.date) -> Session:
    return next(
        s
        for week in plan.weeks
        if not week.recovery and week.start > after
        for s in week.sessions
        if s.template_id == "run_tempo"
    )


def _others(week: Week, dates: set[dt.date]) -> tuple[int, float]:
    others = [s for s in week.sessions if s.date not in dates]
    return sum(s.duration_s for s in others), sum(
        s.tss * (IMPACT if s.sport is Sport.RUN else 1.0) for s in others
    )


# ---------------------------------------------------------------------- scenarios
def test_shorten_a_key_session_holds_its_rung(plan: Plan) -> None:
    key = _session(plan, TEMPO_6)
    assert key is not None and key.rung == 2 and key.duration_s == 60 * 60
    assert _next_tempo(plan, TEMPO_6).rung == 3
    result = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(minutes=40))
    assert result.errors == [] and _errors(result.plan) == []
    shorter = _session(result.plan, TEMPO_6)
    assert shorter is not None and shorter.duration_s <= 40 * 60
    assert (shorter.pinned, shorter.amendment_id, shorter.rung) == (True, "a1", None)
    assert shorter.template_id == "run_tempo" and shorter.intent is Intent.THRESHOLD
    assert "Amended on 2026-11-16: 40 min." in shorter.rationale
    # Below 80% of its length: the ladder does not advance, the next one repeats the rung.
    held = _next_tempo(result.plan, TEMPO_6)
    assert held.rung == 2 and held.name == key.name
    assert _codes(result)[held.date] == "rung_held"
    assert _codes(result)[TEMPO_6] == "amended"
    # No catch-up in the week.
    week_before, week_after = _week(plan, TEMPO_6), _week(result.plan, TEMPO_6)
    assert _others(week_after, {TEMPO_6})[0] <= _others(week_before, {TEMPO_6})[0]
    assert _others(week_after, {TEMPO_6})[1] <= _others(week_before, {TEMPO_6})[1] + 0.5
    log = result.plan.amendments
    assert [(a.id, a.date, a.minutes, a.scope) for a in log] == [("a1", TEMPO_6, 40, Scope.REST)]
    assert log[0].original == key


def test_a_key_kept_at_80_percent_completes_its_rung(plan: Plan) -> None:
    result = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(minutes=50))
    kept = _session(result.plan, TEMPO_6)
    assert kept is not None and 48 * 60 <= kept.duration_s <= 50 * 60
    assert kept.rung == 2
    assert _next_tempo(result.plan, TEMPO_6).rung == 3


def test_mtb_instead_of_the_run(plan: Plan) -> None:
    run = _session(plan, EASY_RUN_5)
    assert run is not None and run.sport is Sport.RUN
    edit = SessionEdit(sport=Sport.BIKE, activity=BikeActivity.MTB)
    result = amend_session(plan, PROFILE, TODAY, EASY_RUN_5, edit)
    assert result.errors == [] and _errors(result.plan) == []
    ride = _session(result.plan, EASY_RUN_5)
    assert ride is not None and (ride.sport, ride.activity_type) == (Sport.BIKE, ActivityType.MTB)
    assert ride.intent is run.intent and ride.duration_s > run.duration_s  # same load, longer
    assert ride.duration_s <= 90 * 60  # fitted to Wednesday's bike time
    week_before, week_after = _week(plan, EASY_RUN_5), _week(result.plan, EASY_RUN_5)
    # The run volume cap is not used by the ride, and the freed run time is not refilled.
    assert week_run_s(week_after) <= week_run_s(week_before) - run.duration_s
    assert sum(s.sport is Sport.RUN for s in week_after.sessions) < sum(
        s.sport is Sport.RUN for s in week_before.sessions
    )


def test_run_instead_of_a_ride(plan: Plan) -> None:
    result = amend_session(plan, PROFILE, TODAY, RIDE_6, SessionEdit(sport=Sport.RUN, minutes=50))
    assert result.errors == [] and _errors(result.plan) == []
    run = _session(result.plan, RIDE_6)
    assert run is not None and (run.sport, run.activity_type) == (Sport.RUN, ActivityType.RUN)
    assert run.duration_s == 50 * 60
    week = _week(result.plan, RIDE_6)
    assert week_run_s(week) <= week_run_s(_week(plan, RIDE_6)) + 60  # the other runs yield
    assert "yielded_to_amendment" in _codes(result).values()


def test_skip_a_key_without_catch_up(plan: Plan) -> None:
    key = _session(plan, TEMPO_9)
    assert key is not None and key.intent.hard
    result = amend_session(plan, PROFILE, TODAY, TEMPO_9, SessionEdit(skip=True))
    assert result.errors == [] and _errors(result.plan) == []
    assert _session(result.plan, TEMPO_9) is None
    assert _codes(result)[TEMPO_9] == "skipped"
    before, after = _week(plan, TEMPO_9), _week(result.plan, TEMPO_9)
    assert (
        sum(s.intent.hard for s in after.sessions)
        == sum(s.intent.hard for s in before.sessions) - 1
    )
    assert after.duration_s <= before.duration_s - key.duration_s
    lost = key.tss * IMPACT
    assert week_weighted_load(after, IMPACT) <= week_weighted_load(before, IMPACT) - lost + 0.5
    assert {s.date for s in after.sessions} <= {s.date for s in before.sessions}
    assert _next_tempo(result.plan, TEMPO_9).rung == key.rung  # rung held
    [amendment] = result.plan.amendments
    assert amendment.skip and amendment.original == key


def test_move_next_to_a_hard_session_makes_the_other_yield(plan: Plan) -> None:
    result = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(move_to=TEMPO_6 + DAY))
    assert result.errors == [] and _errors(result.plan) == []
    moved = _session(result.plan, TEMPO_6 + DAY)
    assert moved is not None and moved.template_id == "run_tempo" and moved.pinned
    codes = _codes(result)
    assert codes[TEMPO_6 + DAY] == "amended" and codes[TEMPO_6] == "moved"
    assert codes[VO2_6] == "yielded_to_amendment"  # Thursday is now < 48 h after Wednesday
    vo2 = next(s for s in _week(result.plan, VO2_6).sessions if s.template_id == "run_vo2max")
    assert (vo2.date - moved.date).days >= 2


def test_easier_intent(plan: Plan) -> None:
    result = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(intent=Intent.ENDURANCE))
    easy = _session(result.plan, TEMPO_6)
    assert easy is not None and easy.intent is Intent.ENDURANCE and not easy.intent.hard
    assert (easy.template_id, easy.rung, easy.name) == ("run_tempo", None, "Easy run")
    vo2 = [s for s in _week(result.plan, TEMPO_6).sessions if s.template_id == "run_vo2max"]
    assert len(vo2) == 1  # the other key session stays; no second tempo session
    assert _next_tempo(result.plan, TEMPO_6).rung == 2  # made easier: rung held
    assert result.errors == []


def test_explicit_minutes_may_exceed_the_day(plan: Plan) -> None:
    longer = amend_session(plan, PROFILE, TODAY, EASY_RUN_5, SessionEdit(minutes=55))
    session = _session(longer.plan, EASY_RUN_5)
    assert session is not None and session.duration_s == 55 * 60  # Wednesday allows 45 min
    assert longer.errors == [] and _errors(longer.plan) == []


def test_safety_caps_are_reported_not_bypassed(plan: Plan) -> None:
    edit = SessionEdit(sport=Sport.BIKE, activity=BikeActivity.MTB, minutes=150)
    result = amend_session(plan, PROFILE, TODAY, LONG_6, edit, force=True)
    assert [i.code for i in result.errors] == ["long_session_ramp"]
    assert result.amendment is not None and result.amendment.force
    assert "long_session_ramp" in {i.code for i in validate_plan(result.plan, PROFILE)}


def test_unavailable_range_equals_the_same_f2_exception(plan: Plan) -> None:
    exception = {"from": "2026-12-21", "to": "2026-12-27", "mode": "limited"}
    exception["minutes"] = {"run": 0, "bike": 60}  # type: ignore[assignment]
    expected = generate_plan(with_exceptions(PROFILE, [exception]), TODAY)
    for today in (TODAY, dt.date(2026, 12, 10)):
        minutes = {Sport.RUN: 0, Sport.BIKE: 60}
        start, end = dt.date(2026, 12, 21), dt.date(2026, 12, 27)
        result = add_unavailable(plan, PROFILE, today, start, end, minutes)
        assert result.plan.weeks == expected.weeks
        assert result.errors == []
        [amendment] = result.plan.amendments
        assert amendment.kind is AmendmentKind.UNAVAILABLE and amendment.to == end
        assert all(c.code in ("calendar_exception", "rebalanced") for c in result.changes)
    off = add_unavailable(plan, PROFILE, TODAY, dt.date(2026, 12, 21), dt.date(2026, 12, 27))
    off_profile = with_exceptions(
        PROFILE, [{"from": "2026-12-21", "to": "2026-12-27", "mode": "off"}]
    )
    assert off.plan.weeks == generate_plan(off_profile, TODAY).weeks


# ---------------------------------------------------------------------- the log
def test_applying_twice_equals_once(plan: Plan) -> None:
    for edit in (
        SessionEdit(minutes=40),
        SessionEdit(skip=True),
        SessionEdit(sport=Sport.BIKE, minutes=70),
        SessionEdit(move_to=TEMPO_6 + DAY),
    ):
        once = amend_session(plan, PROFILE, TODAY, TEMPO_6, edit)
        date = edit.move_to or TEMPO_6
        twice = amend_session(once.plan, PROFILE, TODAY, date, edit)
        assert twice.plan == once.plan
        assert twice.amendment is None and twice.changes == []


def test_a_later_amendment_supersedes_and_inherits(plan: Plan) -> None:
    mtb = SessionEdit(sport=Sport.BIKE, activity=BikeActivity.MTB)
    first = amend_session(plan, PROFILE, TODAY, EASY_RUN_5, mtb)
    second = amend_session(first.plan, PROFILE, TODAY, EASY_RUN_5, SessionEdit(minutes=60))
    a1, a2 = second.plan.amendments
    assert (a1.superseded_by, a2.replaces, a2.superseded_by) == ("a2", "a1", None)
    assert (a2.sport, a2.activity, a2.minutes) == (Sport.BIKE, BikeActivity.MTB, 60)
    assert a2.original == a1.original  # both apply to the session as planned
    ride = _session(second.plan, EASY_RUN_5)
    assert ride is not None and ride.amendment_id == "a2" and ride.duration_s == 60 * 60
    assert ride.activity_type is ActivityType.MTB
    lines = amendment_lines(second.plan)
    assert "superseded by a2" in lines[0] and "active" in lines[1]
    # Undo the later one: the earlier one is active again.
    undone = undo_amendment(second.plan, PROFILE, TODAY, "a2")
    assert undone.plan == replan(first.plan, PROFILE, TODAY)
    assert [a.id for a in undone.plan.amendments] == ["a1"]
    assert undone.plan.amendments[0].active


def test_undo_equals_a_replan_without_the_amendment(plan: Plan) -> None:
    first = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(minutes=40))
    second = amend_session(first.plan, PROFILE, TODAY, TEMPO_9, SessionEdit(skip=True))
    undone = undo_amendment(second.plan, PROFILE, TODAY, "a2")
    assert undone.plan == first.plan
    assert {c.code for c in undone.changes} >= {"undone"}
    back = undo_amendment(first.plan, PROFILE, TODAY, "a1")
    assert back.plan.weeks == plan.weeks and back.plan.amendments == []


def test_scopes(plan: Plan) -> None:
    session = amend_session(
        plan, PROFILE, TODAY, TEMPO_6, SessionEdit(minutes=40), scope=Scope.SESSION
    )
    assert [c.date for c in session.changes] == [TEMPO_6]
    week = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(minutes=40), scope=Scope.WEEK)
    start = _week(plan, TEMPO_6).start
    assert all(start <= c.date < start + 7 * DAY for c in week.changes)
    assert week.plan.weeks[6:] == plan.weeks[6:]
    rest = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(minutes=40))
    assert any(c.date >= start + 7 * DAY for c in rest.changes)


def test_swap_shortcut_is_a_pinned_session_amendment(plan: Plan) -> None:
    result = swap_sport(plan, PROFILE, TODAY, RIDE_6)
    run = _session(result.plan, RIDE_6)
    assert run is not None and run.sport is Sport.RUN and run.pinned
    assert result.plan.amendments[0].scope is Scope.SESSION
    assert [c.date for c in result.changes] == [RIDE_6]


@pytest.mark.parametrize(
    ("date", "edit", "today", "message"),
    [
        (TEMPO_6, SessionEdit(minutes=40), dt.date(2026, 12, 23), "past days cannot be amended"),
        (TEMPO_6, SessionEdit(skip=True, minutes=30), TODAY, "cannot be combined"),
        (TEMPO_6, SessionEdit(), TODAY, "nothing to change"),
        (TEMPO_6, SessionEdit(intent=Intent.VO2MAX), TODAY, "only makes a session easier"),
        (TEMPO_6, SessionEdit(activity=BikeActivity.MTB), TODAY, "for bike sessions"),
        (TEMPO_6, SessionEdit(move_to=TEMPO_6 + 7 * DAY), TODAY, "not in the week"),
        (dt.date(2026, 12, 21), SessionEdit(minutes=30), TODAY, "no session on 2026-12-21"),
    ],
)
def test_refused_amendments(
    plan: Plan, date: dt.date, edit: SessionEdit, today: dt.date, message: str
) -> None:
    with pytest.raises(AmendError, match=message):
        amend_session(plan, PROFILE, today, date, edit)


def test_refused_unavailable_ranges(plan: Plan) -> None:
    with pytest.raises(AmendError, match="past days"):
        add_unavailable(plan, PROFILE, dt.date(2026, 12, 1), dt.date(2026, 11, 30))
    with pytest.raises(AmendError, match="inside this off range"):
        add_unavailable(plan, PROFILE, TODAY, dt.date(2027, 2, 6), dt.date(2027, 2, 8))
    with pytest.raises(AmendError, match="use --scope week or rest"):
        add_unavailable(plan, PROFILE, TODAY, dt.date(2027, 1, 6), scope=Scope.SESSION)
    with pytest.raises(AmendError, match="no amendment a9"):
        undo_amendment(plan, PROFILE, TODAY, "a9")


def test_a_day_without_bike_needs_explicit_minutes(plan: Plan) -> None:
    profile = with_exceptions(
        PROFILE, [{"from": "2026-12-16", "mode": "limited", "minutes": {"bike": 0}}]
    )
    with pytest.raises(AmendError, match="give --minutes"):
        amend_session(plan, profile, TODAY, EASY_RUN_5, SessionEdit(sport=Sport.BIKE))
    stated = amend_session(
        plan, profile, TODAY, EASY_RUN_5, SessionEdit(sport=Sport.BIKE, minutes=60)
    )
    ride = _session(stated.plan, EASY_RUN_5)
    assert ride is not None and ride.sport is Sport.BIKE and ride.duration_s == 60 * 60


def test_key_on_the_bike_keeps_its_slot_and_long_stays(plan: Plan) -> None:
    """A long run turned into a ride keeps the long slot; no second long session appears."""
    result = amend_session(plan, PROFILE, TODAY, LONG_6, SessionEdit(sport=Sport.BIKE, minutes=110))
    week = _week(result.plan, LONG_6)
    assert [s.date for s in week.sessions if s.slot is Slot.LONG] == [LONG_6]
    assert result.errors == []


def test_plan_show_lists_pins_and_amendments(plan: Plan) -> None:
    from endurance_planner.export.markdown import render_markdown

    result = amend_session(plan, PROFILE, TODAY, TEMPO_6, SessionEdit(minutes=40), reason="work")
    text = render_markdown(result.plan, PROFILE)
    assert "## Amendments" in text
    assert "- a1   2026-12-22 Cruise intervals 4x8 min: 40 min (scope rest" in text
    assert "pinned by a1" in text and text.rstrip().endswith(".")
    assert "## Amendments" not in render_markdown(plan, PROFILE)
