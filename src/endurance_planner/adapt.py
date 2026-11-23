"""Adapting a running plan to what was actually done (F8). Pure library functions.

adapt_plan(plan, profile, history, today):
1. Days before `today` become history: the planned sessions of those days are replaced by the
   completed activities of history.json, as synthetic sessions (`source: "actual"`, one steady
   step whose intensity factor comes from the activity's TSS and duration, so the scheduler
   interface is unchanged). They are kept in Week.planned_sessions (and planned_strength) so
   the rules can compare what was done with the plan, and so that adapting again gives the
   same result.
2. A planned key session done at 80% of its time or more (same day, same sport) completes its
   rung; otherwise it is missed and its ladder holds. A planned strength session counts as done
   when a WeightTraining activity is on its day; otherwise it is removed and its ladder holds.
3. replan.replan_plan rebuilds the current week around the days done and every later week from
   that history; pinned sessions (amendments) are kept. Rules (numbers in rules.py):
   - missed key session: it may go to a later free day of its week if the 48 h spacing and the
     long-session rule allow it, else it is dropped; never stacked, never moved to another week;
   - no catch-up: what was not done this week is not added to its remaining days;
   - overreach guard: ramp references use min(actual, planned x 1.10);
   - under-training: after a week below 70% of plan, the next week's load is capped at the
     reference before it;
   - return from break: 7 days or more without any activity are read as days off, so the F2
     return-from-break caps apply.
   These days off are kept in Plan.days_off, so later replans and amendments see them.
4. Optional (preferences.adaptation): readiness gating from wellness (HRV against its 28-day
   mean, or TSB) eases the next key session one rung, or to endurance, as an amendment with
   the reason "low readiness"; SICK, INJURED and HOLIDAY calendar events (UNVERIFIED names) are
   read as days off. This is a training-load heuristic, not health advice.

Nothing is pushed. Every changed day is listed with a reason code (rules.CHANGE_REASONS).
"""

import datetime as dt
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from endurance_planner.amend import AmendError, SessionEdit, amend_session
from endurance_planner.load import run_impact_multiplier, week_weighted_load
from endurance_planner.models.enums import (
    ActivityType,
    AmendmentKind,
    Intent,
    Priority,
    Scope,
    Severity,
    Slot,
)
from endurance_planner.models.history import DayObservation, History, WellnessDay
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import (
    Plan,
    Session,
    Step,
    StrengthSession,
    Target,
    Week,
    flatten,
)
from endurance_planner.models.profile import CalendarException, Profile
from endurance_planner.replan import Change, diff_plans, replan_plan
from endurance_planner.rules import (
    ACTUAL_IF_RANGE,
    CALENDAR_OFF_CATEGORIES,
    KEY_COMPLETED_SHARE,
    OVERREACH_FACTOR,
    READINESS_BASELINE_DAYS,
    READINESS_DAYS,
    READINESS_HRV_RATIO,
    READINESS_MIN_BASELINE,
    READINESS_TSB,
    RETURN_FROM_BREAK,
    REVIEW_HARD_IF,
    planned_version,
    under_trained,
)
from endurance_planner.targets import NATIVE_KIND
from endurance_planner.templates import TEMPLATES, key_blocks, progress_rung

__all__ = ["AdaptResult", "CalendarEvent", "DayObservation", "adapt_plan", "adjust_plan"]

GAP_MIN_DAYS = min(days for days, _ in RETURN_FROM_BREAK)  # a break: no activity this long


@dataclass(frozen=True)
class CalendarEvent:
    """A calendar event read as days off (preferences.adaptation.read_calendar_events)."""

    start: dt.date
    end: dt.date
    category: str
    name: str = ""


@dataclass(frozen=True)
class AdaptResult:
    plan: Plan
    changes: list[Change]
    issues: list[Issue]  # what adapt saw (warnings) and errors the result introduces
    eased: str | None = None  # amendment id of a readiness downgrade

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]


def _note(code: str, message: str, week: int | None = None) -> Issue:
    return Issue(severity=Severity.WARNING, code=f"adapt_{code}", message=message, week=week)


# ---------------------------------------------------------------------- calendar events
def calendar_events(records: Iterable[Mapping[str, Any]]) -> list[CalendarEvent]:
    """Days-off events among intervals.icu calendar events: categories in
    CALENDAR_OFF_CATEGORIES (UNVERIFIED); others, and unreadable ones, are ignored."""
    events = []
    for record in records:
        category = str(record.get("category") or "").upper()
        if category not in CALENDAR_OFF_CATEGORIES:
            continue
        start = _date(record.get("start_date_local"))
        end = _date(record.get("end_date_local")) or start
        if start is None or end is None:
            continue
        name = str(record.get("name") or "")
        events.append(CalendarEvent(start, max(start, end), category, name))
    return events


def _date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------- actual sessions
def _activity(observations: Sequence[DayObservation]) -> tuple[ActivityType, bool]:
    types = [o.activity_type for o in observations]
    if observations[0].sport is not None and observations[0].sport.value == "run":
        return ActivityType.RUN, any(t.startswith("Virtual") for t in types)
    if "MountainBikeRide" in types:
        return ActivityType.MTB, False
    return ActivityType.RIDE, any(t.startswith("Virtual") for t in types)


def actual_session(observations: Sequence[DayObservation], planned: Session | None) -> Session:
    """The activities of one day and sport as one synthetic session (`source: "actual"`).

    It takes the slot of the session planned that day in the same sport. A planned key
    session done at KEY_COMPLETED_SHARE of its time or more passes on its template and rung
    (the ladder advances); done shorter, it keeps the template (the key session of the week
    is used up) without a rung (the ladder holds)."""
    first = observations[0]
    assert first.sport is not None
    duration = sum(o.duration_s for o in observations)
    load = sum(o.tss for o in observations)
    factor = math.sqrt(load / (duration / 3600 * 100)) if duration and load else 0.0
    factor = min(max(factor, ACTUAL_IF_RANGE[0]), ACTUAL_IF_RANGE[1])
    hard = any((o.intensity_factor or 0) >= REVIEW_HARD_IF for o in observations)
    slot, intent, template_id, rung = Slot.EASY, Intent.ENDURANCE, None, None
    if hard:
        intent = Intent.THRESHOLD
    if planned is not None:
        slot = planned.slot
        if planned.template_id is not None and planned.intent.hard:
            template_id, intent = planned.template_id, planned.intent
            if duration >= KEY_COMPLETED_SHARE * planned.duration_s:
                rung = progress_rung(planned)
    activity, indoor = _activity(observations)
    names = [o.name or o.activity_type for o in observations]
    ids = ", ".join(o.activity_id or "?" for o in observations)
    target = Target(kind=NATIVE_KIND[first.sport], low=round(factor, 3), high=round(factor, 3))
    return Session(
        date=first.date,
        sport=first.sport,
        activity_type=activity,
        slot=slot,
        intent=intent,
        name="Done: " + " + ".join(dict.fromkeys(names)),
        steps=[Step(duration_s=duration, target=target)],
        rationale=f"Completed activity ({first.source} {ids}).",
        template_id=template_id,
        indoor=indoor,
        rung=rung,
        source="actual",
    )


@dataclass
class _Past:
    """What the past days held: planned vs done."""

    missed_keys: list[Session] = field(default_factory=list)
    missed_strength: list[dt.date] = field(default_factory=list)
    replaced: set[dt.date] = field(default_factory=set)  # days whose content changed


def _adapt_week(
    week: Week, observations: Sequence[DayObservation], today: dt.date, past: _Past
) -> Week:
    """The week with its days before `today` replaced by what was done."""
    planned = planned_version(week)
    before = [s for s in planned.sessions if s.date < today]
    groups: dict[tuple[dt.date, str], list[DayObservation]] = {}
    for observation in observations:
        if observation.date < today and observation.sport is not None:
            key = (observation.date, observation.sport.value)
            groups.setdefault(key, []).append(observation)
    done = [
        actual_session(group, next((s for s in before if (s.date, s.sport.value) == key), None))
        for key, group in sorted(groups.items())
    ]
    for session in before:
        if session.template_id is not None and session.intent.hard:
            match = [d for d in done if (d.date, d.sport) == (session.date, session.sport)]
            if not match or match[0].rung is None:
                past.missed_keys.append(session)
    strength_days = {o.date for o in observations if o.category == "strength"}
    planned_strength = [s for s in planned.strength if s.date < today]
    kept_strength: list[StrengthSession] = []
    for strength in planned_strength:
        if strength.date in strength_days:
            kept_strength.append(strength.model_copy(update={"source": "actual"}))
        else:
            past.missed_strength.append(strength.date)
    past.replaced |= {s.date for s in before} | {s.date for s in done}
    if not before and not done and not planned_strength:
        return planned
    return planned.model_copy(
        update={
            "sessions": sorted(
                [*done, *(s for s in planned.sessions if s.date >= today)],
                key=lambda s: (s.date, s.sport.value),
            ),
            "strength": [*kept_strength, *(s for s in planned.strength if s.date >= today)],
            "planned_sessions": before,
            "planned_strength": planned_strength,
        }
    )


# ---------------------------------------------------------------------- days off
def _off_days(
    history: History,
    plan: Plan,
    profile: Profile,
    today: dt.date,
    events: Sequence[CalendarEvent],
) -> tuple[list[CalendarException], set[dt.date], list[Issue]]:
    """Days off added for the replan: gaps of GAP_MIN_DAYS or more without any activity
    before today, and calendar events. Days already covered by an exception are left as
    they are; A-race days never become days off."""
    first = plan.weeks[0].start
    last = plan.weeks[-1].start + dt.timedelta(days=6)
    # Days off of an earlier adapt are found again from the history: not counted as covered.
    availability = plan.model_copy(update={"days_off": []}).effective_profile(profile).availability
    a_races = {g.date for g in profile.goals if g.priority is Priority.A}
    active = {o.date for o in history.observations if o.activity_id is not None}
    days: dict[dt.date, str] = {}
    notes: list[Issue] = []
    run: list[dt.date] = []
    day = first
    while day <= today:
        if day < today and day not in active:
            run.append(day)
            day += dt.timedelta(days=1)
            continue
        if len(run) >= GAP_MIN_DAYS:
            notes.append(
                _note(
                    "gap",
                    f"no activity from {run[0]} to {run[-1]} ({len(run)} days): read as a "
                    "break, so the return-from-break caps apply",
                )
            )
            days.update(dict.fromkeys(run, "no activity recorded (plan adapt)"))
        run = []
        day += dt.timedelta(days=1)
    event_days: set[dt.date] = set()
    for event in events:
        notes.append(
            _note("calendar_event", f"{event.category} {event.start} to {event.end}: days off")
        )
        current = event.start
        while current <= event.end:
            if first <= current <= last:
                days[current] = f"{event.category.lower()} (calendar event)"
                event_days.add(current)
            current += dt.timedelta(days=1)
    exceptions: list[CalendarException] = []
    for date in sorted(days):
        if date in a_races or availability.exception_on(date) is not None:
            continue
        previous = exceptions[-1] if exceptions else None
        if (
            previous is not None
            and previous.last + dt.timedelta(days=1) == date
            and (previous.note == days[date])
        ):
            exceptions[-1] = previous.model_copy(update={"end": date})
            continue
        exceptions.append(CalendarException(start=date, mode="off", note=days[date]))
    return exceptions, event_days, notes


# ---------------------------------------------------------------------- readiness
def low_readiness(wellness: Sequence[WellnessDay], today: dt.date) -> str | None:
    """Why readiness is low on READINESS_DAYS consecutive days ending today or yesterday
    (None: it is not). HRV below READINESS_HRV_RATIO of its mean over the previous
    READINESS_BASELINE_DAYS days, or TSB = CTL - ATL below READINESS_TSB."""
    by_date = {day.date: day for day in wellness}
    for end in (today, today - dt.timedelta(days=1)):
        days = [end - dt.timedelta(days=offset) for offset in range(READINESS_DAYS)]
        reasons = [_low(by_date, date) for date in days]
        if all(reasons):
            pairs = zip(days[::-1], reasons[::-1], strict=True)
            return "; ".join(f"{date}: {why}" for date, why in pairs)
    return None


def _low(by_date: Mapping[dt.date, WellnessDay], date: dt.date) -> str | None:
    day = by_date.get(date)
    if day is None:
        return None
    if day.hrv is not None:
        baseline = [
            value.hrv
            for offset in range(1, READINESS_BASELINE_DAYS + 1)
            if (value := by_date.get(date - dt.timedelta(days=offset))) and value.hrv is not None
        ]
        if len(baseline) >= READINESS_MIN_BASELINE:
            mean = statistics.fmean(baseline)
            if day.hrv < READINESS_HRV_RATIO * mean:
                return f"HRV {day.hrv:g} below {READINESS_HRV_RATIO:.0%} of its mean ({mean:.0f})"
    if day.ctl is not None and day.atl is not None and day.ctl - day.atl < READINESS_TSB:
        return f"TSB {day.ctl - day.atl:.0f} below {READINESS_TSB:g}"
    return None


READINESS_REASON = "low readiness"


def _ease_next_key(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    why: str,
    strength_held: Sequence[dt.date],
) -> tuple[Plan, list[Issue], str | None]:
    """The next key session eased one rung (or to endurance at the first rung) through an
    amendment (reason "low readiness"), then the later weeks rebuilt; never harder. A key
    session already eased for low readiness from today on is enough (adapting again changes
    nothing)."""
    eased = [
        a
        for a in plan.active_amendments(AmendmentKind.SESSION)
        if a.reason.startswith(READINESS_REASON) and a.date >= today
    ]
    if eased:
        return plan, [], eased[-1].id
    key = next(
        (
            s
            for week in plan.weeks
            for s in week.sessions
            if s.date >= today and s.intent.hard and not s.actual and not s.pinned
        ),
        None,
    )
    if key is None:
        return plan, [], None
    edit = _easier(key)
    reason = f"{READINESS_REASON} ({why}); plan adapt"
    try:  # scope session: the later weeks are rebuilt below, once the rung is held
        result = amend_session(
            plan, profile, today, key.date, edit, scope=Scope.SESSION, reason=reason
        )
    except AmendError:
        return plan, [], None
    if result.amendment is None:
        return plan, [], None
    amendment_id = result.amendment.id
    weeks = [  # an eased key session does not complete its rung: the ladder holds
        w.model_copy(
            update={
                "sessions": [
                    s.model_copy(update={"rung": None}) if s.amendment_id == amendment_id else s
                    for s in w.sessions
                ]
            }
        )
        for w in result.plan.weeks
    ]
    rebuilt = replan_plan(
        result.plan.model_copy(update={"weeks": weeks}),
        profile,
        today,
        strength_held=strength_held,
        baseline=plan,
    )
    if rebuilt.errors:
        return plan, [], None
    note = _note(
        "readiness",
        f"{key.date} {key.name}: eased ({edit.describe()}) after {READINESS_DAYS} low-readiness "
        f"days ({why}). A training-load heuristic, not health advice.",
    )
    return rebuilt.plan, [note], amendment_id


def _easier(key: Session) -> SessionEdit:
    """One rung down (the minutes of the rung below, shorter than any version of this rung),
    or endurance when the session is at its first rung."""
    template = TEMPLATES.get(key.template_id or "")
    level = progress_rung(key)
    if template is None or not level:
        return SessionEdit(intent=Intent.ENDURANCE)

    def length(rung: int, short: bool) -> int:
        return sum(step.duration_s for step in flatten(key_blocks(template, rung, short=short)))

    target = min(length(level - 1, False), length(level, True) - 60, key.duration_s - 60)
    if target < 20 * 60:
        return SessionEdit(intent=Intent.ENDURANCE)
    return SessionEdit(minutes=target // 60)


# ---------------------------------------------------------------------- entry points
def adapt_plan(
    plan: Plan,
    profile: Profile,
    history: History,
    today: dt.date,
    *,
    events: Sequence[CalendarEvent] = (),
) -> AdaptResult:
    """The plan rebuilt from what was done before `today` (see the module docstring).
    Deterministic: the same plan, profile, history, events and day give the same result."""
    past = _Past()
    weeks = []
    for week in plan.weeks:
        end = week.start + dt.timedelta(days=6)
        if week.start >= today:
            weeks.append(planned_version(week) if week.has_actual else week)
            continue
        weeks.append(_adapt_week(week, history.between(week.start, end), today, past))
    adaptation = profile.preferences.adaptation
    extra, event_days, notes = _off_days(
        history, plan, profile, today, events if adaptation.read_calendar_events else ()
    )
    staged = plan.model_copy(update={"weeks": weeks, "days_off": extra})
    issues = list(notes)
    if history.synced is None or history.synced < today - dt.timedelta(days=1):
        issues.append(
            _note(
                "history_stale",
                f"history last synced {history.synced or 'never'}: run `sync` first, or the "
                "days since count as not trained",
            )
        )
    impact = run_impact_multiplier(profile)
    for week in staged.weeks:
        if not week.has_actual:
            continue
        planned_load = week_weighted_load(planned_version(week), impact)
        load = week_weighted_load(week, impact)
        if week.start + dt.timedelta(days=7) <= today and under_trained(week, impact):
            issues.append(
                _note(
                    "under_training",
                    f"week {week.index}: load {load:.0f} done of {planned_load:.0f} planned; "
                    "no catch-up, the next week is capped at the reference before it",
                    week.index,
                )
            )
        elif planned_load and load > OVERREACH_FACTOR * planned_load:
            issues.append(
                _note(
                    "overreach",
                    f"week {week.index}: load {load:.0f} done for {planned_load:.0f} planned; "
                    f"the next caps use at most {OVERREACH_FACTOR:.0%} of plan",
                    week.index,
                )
            )
    for key in past.missed_keys:
        issues.append(
            _note("missed_key", f"{key.date} {key.name}: not done (or under 80%); rung held")
        )
    missed_dates = {key.date for key in past.missed_keys}
    missed_templates = {key.template_id for key in past.missed_keys}
    current = next(
        (w for w in staged.weeks if w.start <= today <= w.start + dt.timedelta(days=6)), None
    )

    def reason_of(day: dt.date) -> str | None:
        if day in event_days:
            return "calendar_event"
        if day < today:
            return "missed_key" if day in missed_dates else "actual"
        if current is not None and current.start <= day <= current.start + dt.timedelta(6):
            return "adapted"
        return None

    result = replan_plan(
        staged,
        profile,
        today,
        reason_of=reason_of,
        default_reason="rebalanced",
        baseline=plan,
        strength_held=past.missed_strength,
    )
    adapted = result.plan.model_copy(update={"goals": plan.goals, "athlete": plan.athlete})
    issues += result.issues
    eased_id = None
    if adaptation.use_wellness:
        why = low_readiness(history.wellness, today)
        if why is not None:
            adapted, eased, eased_id = _ease_next_key(
                adapted, profile, today, why, past.missed_strength
            )
            adapted = adapted.model_copy(update={"goals": plan.goals, "athlete": plan.athlete})
            issues += eased
    eased_dates = {
        s.date
        for w in adapted.weeks
        for s in w.sessions
        if eased_id is not None and s.amendment_id == eased_id
    }

    def final_reason(day: dt.date) -> str | None:
        return "readiness" if day in eased_dates else reason_of(day)

    changes = diff_plans(plan, adapted, final_reason, "rebalanced")
    changes = [
        Change(c.date, "missed_key_moved", c.before, c.after)
        if c.code == "adapted"
        and isinstance(c.after, Session)
        and c.after.template_id in missed_templates
        and c.after.template_id is not None
        else c
        for c in changes
    ]
    return AdaptResult(adapted, changes, issues, eased_id)


def adjust_plan(
    plan: Plan, profile: Profile, observations: list[DayObservation], today: dt.date
) -> Plan:
    """The plan adapted to `observations` (completed activities, synced today); see
    adapt_plan."""
    history = History(synced=today, observations=list(observations))
    return adapt_plan(plan, profile, history, today).plan
