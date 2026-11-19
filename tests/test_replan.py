"""F3: replanning a running plan (replan.py) and pushing only what changed."""

import datetime as dt

import pytest

from builders import TODAY, make_profile, mtb_marathon_profile, run_10k_profile, with_exceptions
from endurance_planner.amend import SessionEdit, amend_session
from endurance_planner.export.intervals_icu import push_plan
from endurance_planner.export.json_io import load_plan
from endurance_planner.models.enums import Severity
from endurance_planner.models.plan import Plan, Week
from endurance_planner.models.profile import Profile
from endurance_planner.planner import generate_plan
from endurance_planner.replan import ReplanError, replan, replan_plan
from endurance_planner.validation import validate_plan
from test_intervals_icu import FakeApi, _client
from test_regression import V1_PLAN

DAY = dt.timedelta(days=1)


def _without_rungs(weeks: list[Week]) -> list[list[object]]:
    return [[s.model_copy(update={"rung": None}) for s in week.sessions] for week in weeks]


@pytest.mark.parametrize(
    "profile", [run_10k_profile(), mtb_marathon_profile()], ids=["10k", "mtb_marathon"]
)
def test_an_unchanged_plan_is_replanned_identically(profile: Profile) -> None:
    """Seeding the scheduler from the weeks already done reproduces every later week."""
    plan = generate_plan(profile, TODAY)
    assert replan(plan, profile, TODAY) == plan  # nothing done yet: same plan, same warnings
    for week in plan.weeks[1:]:
        for offset in (0, 3):  # on a Monday, and in the middle of a week
            today = week.start + offset * DAY
            result = replan_plan(plan, profile, today)
            assert result.plan.weeks == plan.weeks, today
            assert result.changes == [] and result.errors == []


def test_a_v1_plan_can_be_replanned() -> None:
    """Rungs of v1 sessions are read back from their names."""
    profile = make_profile()
    stored = load_plan(V1_PLAN)
    result = replan_plan(stored, profile, dt.date(2027, 1, 13))
    assert _without_rungs(result.plan.weeks) == _without_rungs(generate_plan(profile, TODAY).weeks)
    assert validate_plan(stored, profile) == validate_plan(generate_plan(profile, TODAY), profile)


def test_replan_twice_gives_the_same_plan() -> None:
    profile = run_10k_profile()
    plan = generate_plan(profile, TODAY)
    today = dt.date(2026, 12, 9)
    amended = amend_session(plan, profile, today, dt.date(2026, 12, 22), SessionEdit(skip=True))
    edited = with_exceptions(profile, [{"from": "2027-01-04", "to": "2027-01-06", "mode": "off"}])
    once = replan(amended.plan, edited, today)
    assert replan(once, edited, today) == once
    assert replan(amended.plan, profile, today) == amended.plan


def test_replan_after_editing_the_profile_keeps_the_past_and_the_pins() -> None:
    profile = run_10k_profile()
    plan = generate_plan(profile, TODAY)
    today = dt.date(2026, 12, 10)  # Thursday of week 4
    amended = amend_session(plan, profile, today, dt.date(2027, 1, 12), SessionEdit(minutes=40))
    trip = {"from": "2026-12-21", "to": "2026-12-27", "mode": "off", "note": "ski week"}
    edited = with_exceptions(profile, [trip])
    result = replan_plan(amended.plan, edited, today)
    assert result.errors == []
    assert validate_plan(result.plan, edited) == [
        i for i in validate_plan(result.plan, edited) if i.severity is Severity.WARNING
    ]
    new = result.plan
    assert new.weeks[:3] == amended.plan.weeks[:3]  # weeks that are over
    past = [s for s in amended.plan.weeks[3].sessions if s.date < today]
    assert [s for s in new.weeks[3].sessions if s.date < today] == past  # done this week
    pinned = [s for w in amended.plan.weeks for s in w.sessions if s.pinned]
    assert [s for w in new.weeks for s in w.sessions if s.pinned] == pinned
    assert new.weeks[5].sessions == []  # the ski week
    codes = {c.code for c in result.changes}
    assert "calendar_exception" in codes and "replanned" in codes
    assert all(c.date >= today for c in result.changes)
    assert (new.id, new.created) == (plan.id, plan.created)


def test_a_moved_goal_stops_the_replan() -> None:
    profile = run_10k_profile()
    plan = generate_plan(profile, TODAY)
    data = profile.model_dump(mode="json", by_alias=True)
    data["goals"][0]["date"] = "2027-02-14"  # one week later: recovery weeks shift
    moved = Profile.model_validate(data)
    assert plan.weeks[3].recovery  # week 4, already done on 2026-12-16
    with pytest.raises(ReplanError, match="skeleton_changed") as error:
        replan(plan, moved, dt.date(2026, 12, 16))
    assert error.value.code == "skeleton_changed"
    # The first weeks still match: replanning from week 3 is fine.
    later = replan(plan, moved, dt.date(2026, 12, 2))
    assert later.weeks[-1].races == ["10k"] and later.weeks[:2] == plan.weeks[:2]
    # Before the start nothing is done yet, so a new structure is fine.
    assert replan(plan, moved, TODAY).weeks[-1].start == dt.date(2027, 2, 8)


def test_history_replaces_the_past_weeks() -> None:
    profile = run_10k_profile()
    plan = generate_plan(profile, TODAY)
    today = plan.weeks[4].start
    done = [week.model_copy(update={"sessions": week.sessions[:1]}) for week in plan.weeks[:4]]
    result = replan_plan(plan, profile, today, history=done)
    assert result.plan.weeks[:4] == done
    errors = [i for i in validate_plan(result.plan, profile) if i.severity is Severity.ERROR]
    assert all(i.week is not None and i.week <= 4 for i in errors)  # only in what was done
    with pytest.raises(ValueError, match="history"):
        replan_plan(plan, profile, today, history=done[:2])


def test_push_after_an_amendment_updates_only_the_changed_events() -> None:
    profile = run_10k_profile()
    plan = generate_plan(profile, TODAY)
    api = FakeApi()
    client, _ = _client(api)
    push_plan(client, plan)
    ids = {event["external_id"]: key for key, event in api.events.items()}
    count = len(api.events)
    result = amend_session(plan, profile, TODAY, dt.date(2026, 12, 22), SessionEdit(minutes=40))
    assert result.plan.id == plan.id
    requests = len(api.requests)
    pushed = push_plan(client, result.plan, only_changed=True)
    old = {s.date: s for w in plan.weeks for s in w.sessions}
    new = {s.date: s for w in result.plan.weeks for s in w.sessions}
    changed = [d for d in new if d not in old or old[d] != new[d]]
    removed = [d for d in old if d not in new or old[d].slot is not new[d].slot]
    assert pushed.upserted == len(changed) and pushed.upserted < count / 2
    assert pushed.unchanged == len(new) - len(changed)
    assert pushed.pruned == len(removed)
    upserts = [r for r in api.requests[requests:] if r.method == "POST"]
    assert len(upserts) == 1
    # Unchanged sessions kept their external_id and their calendar event.
    for date, session in new.items():
        if date in old and old[date] == session:
            key = ids[f"endurance-planner:{plan.id}:{date}:{session.slot.value}"]
            assert api.events[key]["name"] == session.name
    again = push_plan(client, result.plan, only_changed=True)
    assert (again.upserted, again.pruned, again.unchanged) == (0, 0, len(new))


def test_replanning_an_ended_plan_changes_nothing() -> None:
    profile = run_10k_profile()
    plan: Plan = generate_plan(profile, TODAY)
    late = plan.weeks[-1].start + 8 * DAY
    result = replan_plan(plan, profile, late)
    assert result.plan.weeks == plan.weeks and result.changes == []
