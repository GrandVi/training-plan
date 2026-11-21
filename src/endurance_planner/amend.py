"""Amendments: changes to a running plan decided along the way (pure library functions).

An amendment edits, skips or moves one planned session, or adds an unavailable date range.
The amended session is pinned (Session.pinned, Session.amendment_id): a replan never
touches it. Every amendment is logged in Plan.amendments; a later amendment of the same
session supersedes the earlier one (both stay in the log) and keeps its flags unless they
are given again. profile.yaml is never written; nothing is pushed.

How the plan follows (numbers and reason codes in rules.py):
- scope `session` changes only that session and re-validates the plan (like `plan swap`);
  `week` also rebuilds the unpinned sessions of that week; `rest` (default) also rebuilds
  every later week, because ramp references and ladder progress change.
- shorter: a key session goes down its ladder until it fits (then its main set is scaled,
  then it becomes plain endurance, with a warning); other sessions are resized.
- other sport or bike type: the swap conversion (same intent, same impact-weighted load);
  a technical session cannot keep its intent and becomes endurance, with a warning.
- no catch-up: what a skipped or shortened session loses is not made up elsewhere.
- rung hold: a key session skipped, made easier or kept under 80% of its length does not
  advance its ladder; the next one repeats the rung.
- unpinned sessions yield to the amended one (other day, lower rung or easy session).
- explicit means allowed: an amended session may exceed the day's usual minutes, never a
  safety cap; the errors it causes are returned and the caller decides (--force).
- amendments of past days are refused.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, fields

from endurance_planner.load import is_low_intensity, run_impact_multiplier
from endurance_planner.models.enums import (
    ActivityType,
    AmendmentKind,
    BikeActivity,
    Intent,
    Phase,
    Priority,
    Scope,
    Severity,
    Slot,
    Sport,
    StepKind,
)
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Amendment, Plan, Repeat, Session, Step, Target, Week
from endurance_planner.models.profile import Goal, Profile
from endurance_planner.replan import (
    Change,
    ReasonOf,
    diff_plans,
    introduced_issues,
    replan_plan,
    week_index,
)
from endurance_planner.rules import (
    DEFAULT_SCOPE,
    EASIER_INTENTS,
    KEY_COMPLETED_SHARE,
    KEY_MIN_SCALE,
)
from endurance_planner.scheduler import LONG_SLOTS, Scheduler, race_pace
from endurance_planner.swap import SwapError, _scale_blocks, convert_for_day, convert_session
from endurance_planner.targets import NATIVE_KIND, express_blocks, resolve_session
from endurance_planner.templates import (
    EASY,
    RECOVERY,
    TEMPLATES,
    Template,
    key_blocks,
    long_blocks,
    progress_rung,
    rung_label,
)
from endurance_planner.thresholds import ResolvedThresholds, resolve_thresholds
from endurance_planner.zones import zone_set

EASY_NAMES = {
    (Sport.BIKE, Intent.ENDURANCE): "Endurance ride",
    (Sport.RUN, Intent.ENDURANCE): "Easy run",
    (Sport.BIKE, Intent.RECOVERY): "Recovery ride",
    (Sport.RUN, Intent.RECOVERY): "Recovery run",
}


class AmendError(ValueError):
    """The amendment cannot be made; the message says why."""


class NoSession(AmendError):
    """There is no session (and no skipped one) on that date."""


@dataclass(frozen=True)
class SessionEdit:
    """What to change in one session (the flags of `plan amend`). Flags combine, except
    `skip`."""

    minutes: int | None = None
    sport: Sport | None = None
    activity: BikeActivity | None = None
    intent: Intent | None = None
    skip: bool = False
    move_to: dt.date | None = None

    def describe(self) -> str:
        if self.skip:
            return "skipped"
        parts = []
        if self.sport is not None:
            parts.append(f"as a {self.sport.value}")
        if self.activity is not None:
            parts.append(f"on the {self.activity.value} bike")
        if self.intent is not None:
            parts.append(f"made {self.intent.value}")
        if self.minutes is not None:
            parts.append(f"{self.minutes} min")
        if self.move_to is not None:
            parts.append(f"moved to {self.move_to}")
        return ", ".join(parts)


@dataclass(frozen=True)
class AmendResult:
    plan: Plan
    amendment: Amendment | None  # None: nothing to do (the same amendment is already active)
    changes: list[Change]
    issues: list[Issue]  # conversion warnings, new warnings, and errors the change introduced

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]


def edit_of(amendment: Amendment) -> SessionEdit:
    return SessionEdit(
        minutes=amendment.minutes,
        sport=amendment.sport,
        activity=amendment.activity,
        intent=amendment.intent,
        skip=amendment.skip,
        move_to=amendment.move_to,
    )


def _merge(base: Amendment | None, edit: SessionEdit) -> SessionEdit:
    """A later amendment of the same session keeps the earlier flags it does not change."""
    if edit.skip or base is None or base.skip:
        return edit
    before = edit_of(base)
    same_sport = edit.sport is None or edit.sport is before.sport
    return SessionEdit(
        minutes=edit.minutes or before.minutes,
        sport=edit.sport or before.sport,
        activity=edit.activity or (before.activity if same_sport else None),
        intent=edit.intent or before.intent,
        move_to=edit.move_to or before.move_to,
    )


def _check_edit(edit: SessionEdit) -> None:
    others = [f.name for f in fields(edit) if f.name != "skip" and getattr(edit, f.name)]
    if edit.skip and others:
        raise AmendError(f"--skip cannot be combined with other changes ({', '.join(others)})")
    if not edit.skip and not others:
        raise AmendError(
            "nothing to change: give --minutes, --sport, --activity, --intent, --move-to or --skip"
        )
    if edit.intent is not None and edit.intent not in EASIER_INTENTS:
        allowed = " or ".join(intent.value for intent in EASIER_INTENTS)
        raise AmendError(f"--intent only makes a session easier: {allowed}")
    if edit.activity is not None and edit.sport is Sport.RUN:
        raise AmendError("--activity is for bike sessions")


# ---------------------------------------------------------------------- one session
def _mode(profile: Profile, sport: Sport) -> str:
    mode = profile.thresholds.target_mode
    return mode.bike if sport is Sport.BIKE else mode.run


def _target(sport: Sport, low: float, high: float) -> Target:
    return Target(kind=NATIVE_KIND[sport], low=low, high=high)


def easy_version(session: Session, intent: Intent, duration_s: int, profile: Profile) -> Session:
    """The session as a steady endurance or recovery session of the given length.

    template_id is kept: it says which key session of the week this one replaces (its slot
    is not given to another key session), while the missing rung keeps its ladder in place.
    """
    sport = session.sport
    low, high = (EASY if intent is Intent.ENDURANCE else RECOVERY)[sport]
    steps = express_blocks(
        [Step(duration_s=duration_s, target=_target(sport, low, high))],
        sport,
        _mode(profile, sport),
    )
    name = EASY_NAMES[(sport, intent)]
    if session.slot in LONG_SLOTS and intent is Intent.ENDURANCE:
        name = Scheduler._long_name(sport, duration_s, False, session.slot is Slot.LONG_2)
    return session.model_copy(
        update={
            "steps": steps,
            "intent": intent,
            "name": name,
            "technical": False,
            "notes": "",
        }
    )


def _scale_to(session: Session, target_s: int) -> Session:
    """All steps scaled so that the session lasts about target_s (never more)."""
    factor = target_s / session.duration_s
    scaled = session
    for _ in range(6):
        scaled = session.model_copy(update={"steps": _scale_blocks(session.steps, factor)})
        if scaled.duration_s <= target_s:
            break
        factor *= 0.98
    return scaled


def _key_variant(
    session: Session,
    template: Template,
    level: int,
    short: bool,
    thresholds: ResolvedThresholds,
    goal: Goal | None,
    profile: Profile,
) -> Session:
    """The key session rebuilt at another rung of its ladder, in the session's sport."""
    pace = race_pace(goal, thresholds)[0] if goal is not None else None
    blocks = key_blocks(
        template, level, short=short, pace_s=thresholds.threshold_pace_s, goal_pace=pace
    )
    variant = session.model_copy(
        update={
            "sport": template.sport,
            "steps": express_blocks(blocks, template.sport, _mode(profile, template.sport)),
            "name": f"{template.name} {rung_label(template, level)}",
        }
    )
    if session.sport is not template.sport:
        variant, _ = convert_session(
            variant,
            run_impact_multiplier(profile),
            _mode(profile, session.sport),
            zone_set(profile.thresholds.zone_models),
        )
        variant = variant.model_copy(
            update={"activity_type": session.activity_type, "indoor": session.indoor}
        )
    return variant


def fit_minutes(
    session: Session,
    target_s: int,
    profile: Profile,
    thresholds: ResolvedThresholds,
    goal: Goal | None,
) -> tuple[Session, list[tuple[str, str]]]:
    """The session fitted to target_s, with notes (kind, message).

    Shorter key session: down its ladder (shortened warm-up and cool-down included), then the
    main set of the first rung scaled down to KEY_MIN_SCALE, else plain endurance. Longer key
    session: easy volume added before the cool-down. Long sessions are rebuilt at the new
    length; other sessions are resized step by step.
    """
    current = session.duration_s
    if current == target_s:
        return session, []
    where = f"{session.date} {session.name}"
    template = TEMPLATES.get(session.template_id or "")
    if template is not None and session.intent is template.intent:
        if target_s < current:
            level = progress_rung(session) or 0
            smallest = session
            for lv in range(level, -1, -1):
                for short in (False, True):
                    smallest = _key_variant(session, template, lv, short, thresholds, goal, profile)
                    if smallest.duration_s <= target_s:
                        return smallest, [("shortened", f"{where}: now {smallest.name}")]
            if target_s >= KEY_MIN_SCALE * smallest.duration_s:
                scaled = _scale_to(smallest, target_s)
                scaled = scaled.model_copy(update={"name": f"{smallest.name}, shorter reps"})
                return scaled, [("shortened", f"{where}: first rung with shorter reps")]
            easy = easy_version(session, Intent.ENDURANCE, target_s, profile)
            message = (
                f"{where}: {target_s // 60} min is too short to keep its intent; "
                "now plain endurance"
            )
            return easy, [("intent_lost", message)]
        low, high = EASY[session.sport]
        extra = express_blocks(
            [Step(duration_s=target_s - current, target=_target(session.sport, low, high))],
            session.sport,
            _mode(profile, session.sport),
        )
        steps: list[Step | Repeat] = list(session.steps)
        last = steps[-1]
        cooldown = isinstance(last, Step) and last.kind is StepKind.COOLDOWN
        at = len(steps) - 1 if cooldown else len(steps)
        steps[at:at] = extra
        longer = session.model_copy(update={"steps": steps})
        return longer, [("lengthened", f"{where}: easy volume added before the cool-down")]
    if session.slot in LONG_SLOTS and session.template_id is None:
        zones = zone_set(profile.thresholds.zone_models)
        quality = any(
            not is_low_intensity(step.target, session.sport, zones) for step in session.flat_steps()
        )
        blocks = long_blocks(session.sport, target_s, quality)
        steps = express_blocks(blocks, session.sport, _mode(profile, session.sport))
        quality = quality and len(blocks) > 1
        second = session.slot is Slot.LONG_2
        name = Scheduler._long_name(session.sport, target_s, quality, second)
        return session.model_copy(update={"steps": steps, "name": name}), []
    return _scale_to(session, target_s), []


def amended_session(
    original: Session,
    edit: SessionEdit,
    profile: Profile,
    thresholds: ResolvedThresholds,
    goal: Goal | None,
    amendment_id: str,
    today: dt.date,
) -> tuple[Session | None, list[tuple[str, str]]]:
    """The planned session with the edit applied, pinned (None when skipped), and notes
    (kind, message). `profile` should include the plan's unavailable ranges."""
    if edit.skip:
        return None, []
    session = original
    notes: list[tuple[str, str]] = []
    date = edit.move_to or original.date
    explicit = edit.minutes is not None
    planned_s = original.duration_s  # planned length, in the session's final sport
    if edit.sport is not None and edit.sport is not original.sport:
        try:
            session, converted = convert_for_day(session, profile, date=date, fit=not explicit)
            planned_s = convert_for_day(original, profile, date=date, fit=False)[0].duration_s
        except SwapError as exc:
            hint = " (give --minutes to state the time you have)" if "time on" in str(exc) else ""
            raise AmendError(f"{exc}{hint}") from exc
        notes += converted
    if edit.activity is not None:
        if session.sport is not Sport.BIKE:
            raise AmendError("--activity is for bike sessions")
        if session.technical and edit.activity is not BikeActivity.MTB:
            message = f"{original.date} {session.name}: skills work needs the MTB; now endurance"
            notes.append(("intent_lost", message))
            session = easy_version(session, Intent.ENDURANCE, session.duration_s, profile)
        mtb = edit.activity is BikeActivity.MTB
        session = session.model_copy(
            update={
                "activity_type": ActivityType.MTB if mtb else ActivityType.RIDE,
                "indoor": edit.activity is BikeActivity.TRAINER,
            }
        )
    if edit.intent is not None:
        zones = zone_set(profile.thresholds.zone_models)
        intense = any(
            not is_low_intensity(s.target, session.sport, zones) for s in session.flat_steps()
        )
        if edit.intent is not session.intent or intense:
            session = easy_version(session, edit.intent, session.duration_s, profile)
    if edit.minutes is not None:
        session, fitted = fit_minutes(session, edit.minutes * 60, profile, thresholds, goal)
        notes += fitted

    planned = progress_rung(original)
    rung = None
    if planned is not None and original.template_id is not None:
        completed = (
            session.template_id == original.template_id
            and session.intent is original.intent
            and session.duration_s >= KEY_COMPLETED_SHARE * planned_s
        )
        rung = planned if completed else None
    note = f"Amended on {today}: {edit.describe()}."
    session = session.model_copy(
        update={
            "date": date,
            "rung": rung,
            "pinned": True,
            "amendment_id": amendment_id,
            "rationale": f"{session.rationale} {note}",
        }
    )
    return resolve_session(session, thresholds), notes


# ---------------------------------------------------------------------- the plan
def _next_id(plan: Plan) -> str:
    numbers = [int(a.id[1:]) for a in plan.amendments if a.id[1:].isdigit()]
    return f"a{max(numbers, default=0) + 1}"


def _goal_of(plan: Plan, date: dt.date) -> Goal | None:
    index = week_index(plan, date)
    if not 0 <= index < len(plan.weeks):
        return None
    return next((g for g in plan.goals if g.name == plan.weeks[index].goal), None)


def _with_sessions(plan: Plan, drop: Sequence[Session], add: Sequence[Session]) -> list[Week]:
    """Weeks without the `drop` sessions (by identity) and with `add` in their weeks."""
    ids = {id(session) for session in drop}
    weeks = []
    for week in plan.weeks:
        end = week.start + dt.timedelta(days=6)
        sessions = [s for s in week.sessions if id(s) not in ids]
        sessions += [s for s in add if week.start <= s.date <= end]
        weeks.append(week.model_copy(update={"sessions": sorted(sessions, key=lambda s: s.date)}))
    return weeks


def _issue(kind: str, message: str, week: int | None) -> Issue:
    return Issue(severity=Severity.WARNING, code=f"amend_{kind}", message=message, week=week)


def _apply(
    plan: Plan,
    staged: Plan,
    profile: Profile,
    today: dt.date,
    scope: Scope,
    weeks: tuple[int, int],
    reason_of: ReasonOf,
    notes: list[Issue],
) -> tuple[Plan, list[Change], list[Issue]]:
    """Rebuild what the scope covers and compare with the plan before the amendment."""
    if scope is Scope.SESSION:
        changes = diff_plans(plan, staged, reason_of, "amended")
        return staged, changes, notes + introduced_issues(plan, staged, profile)
    first, last = weeks
    week_scope = scope is Scope.WEEK
    result = replan_plan(
        staged,
        profile,
        today,
        first_week=first if week_scope else None,
        last_week=last if week_scope else None,
        reason_of=reason_of,
        default_reason="rebalanced",
        baseline=plan,
    )
    return result.plan, result.changes, notes + result.issues


def amend_session(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    date: dt.date,
    edit: SessionEdit,
    *,
    scope: Scope = DEFAULT_SCOPE,
    reason: str = "",
    force: bool = False,
) -> AmendResult:
    """Edit, skip or move the session on `date` and adapt the plan (see the module
    docstring). `force` is recorded in the amendment: the caller saves a plan with errors
    only when the athlete asked for it."""
    _check_edit(edit)
    sessions = [s for week in plan.weeks for s in week.sessions]
    current = next((s for s in sessions if s.date == date), None)
    active = {a.id: a for a in plan.active_amendments(AmendmentKind.SESSION)}
    base: Amendment | None = None
    if current is not None and current.amendment_id is not None:
        base = active.get(current.amendment_id)
        if base is None:
            raise AmendError(f"{date}: amendment {current.amendment_id} is not in the plan's log")
    elif current is None:
        base = next((a for a in active.values() if a.skip and a.date == date), None)
        if base is None:
            raise NoSession(f"no session on {date}")
    if date < today:
        raise AmendError(f"{date} is before today ({today}): past days cannot be amended")
    original = base.original if base is not None else current
    assert original is not None
    merged = _merge(base, edit)
    if base is not None and edit_of(base) == merged:
        message = f"{date}: amendment {base.id} already does this; nothing changed"
        return AmendResult(plan, None, [], [_issue("already_applied", message, None)])
    index = week_index(plan, original.date)
    if merged.move_to is not None:
        if merged.move_to < today:
            raise AmendError(f"--move-to {merged.move_to} is before today ({today})")
        if week_index(plan, merged.move_to) != index:
            raise AmendError(
                f"--move-to {merged.move_to} is not in the week of {original.date}; skip this "
                "session and amend one of the other week instead"
            )
        taken = next(
            (s for s in sessions if s.date == merged.move_to and s.pinned and s is not current),
            None,
        )
        if taken is not None:
            raise AmendError(
                f"{merged.move_to} already has an amended session ({taken.amendment_id})"
            )

    effective = plan.effective_profile(profile)
    thresholds, _ = resolve_thresholds(effective, today)
    amendment_id = _next_id(plan)
    result, notes = amended_session(
        original, merged, effective, thresholds, _goal_of(plan, date), amendment_id, today
    )
    amendment = Amendment(
        id=amendment_id,
        created=today,
        date=original.date,
        skip=merged.skip,
        move_to=merged.move_to,
        minutes=merged.minutes,
        sport=merged.sport,
        activity=merged.activity,
        intent=merged.intent,
        scope=scope,
        reason=reason,
        force=force,
        original=original.model_copy(update={"pinned": False, "amendment_id": None}),
        replaces=base.id if base is not None else None,
    )
    log = [
        a.model_copy(update={"superseded_by": amendment_id}) if base and a.id == base.id else a
        for a in plan.amendments
    ]
    drop = [s for s in sessions if s.date == original.date and s.amendment_id is None]
    drop += [s for s in sessions if base is not None and s.amendment_id == base.id]
    staged = plan.model_copy(
        update={
            "weeks": _with_sessions(plan, drop, [result] if result is not None else []),
            "amendments": [*log, amendment],
        }
    )

    week = plan.weeks[index] if 0 <= index < len(plan.weeks) else None
    week_end = week.start + dt.timedelta(days=6) if week else original.date
    held = None
    if progress_rung(original) is not None and (result is None or result.rung is None):
        held = original.template_id
    held_dates: list[dt.date] = []

    def reason_of(day: dt.date) -> str | None:
        if result is not None and day == result.date:
            return "amended"
        if day == original.date:
            return "skipped" if result is None else "moved"
        if day in held_dates:
            return "rung_held"
        if week is not None and week.start <= day <= week_end:
            return "yielded_to_amendment"
        if day < original.date:
            return "replanned"
        return None

    issues = [_issue(kind, message, index + 1) for kind, message in notes]
    new_plan, _, issues = _apply(
        plan, staged, profile, today, scope, (index, index), reason_of, issues
    )
    if held is not None:
        later = (
            s
            for w in new_plan.weeks
            if w.start > week_end and not w.recovery and w.phase is not Phase.TAPER
            for s in w.sessions
            if s.template_id == held
        )
        first_later = next(later, None)
        if first_later is not None:
            held_dates.append(first_later.date)
    changes = diff_plans(plan, new_plan, reason_of, "rebalanced")
    return AmendResult(new_plan, amendment, changes, issues)


def add_unavailable(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    start: dt.date,
    end: dt.date | None = None,
    minutes: dict[Sport, int] | None = None,
    activity: BikeActivity | None = None,
    *,
    scope: Scope = DEFAULT_SCOPE,
    reason: str = "",
    force: bool = False,
) -> AmendResult:
    """Make a date range unavailable (no `minutes`: no training at all) or limited, as a
    calendar exception kept in the plan, and adapt the plan. Same rules as an exception
    written in profile.yaml."""
    end = end or start
    if start < today:
        raise AmendError(f"{start} is before today ({today}): past days cannot be amended")
    if end < start:
        raise AmendError(f"--to {end} is before --from {start}")
    if scope is Scope.SESSION:
        raise AmendError("an unavailable range rebuilds whole weeks: use --scope week or rest")
    amendment = Amendment(
        id=_next_id(plan),
        kind=AmendmentKind.UNAVAILABLE,
        created=today,
        date=start,
        to=end if end != start else None,
        day_minutes=dict(minutes or {}),
        activity=activity,
        scope=scope,
        reason=reason,
        force=force,
    )
    try:
        exception = amendment.as_exception()
    except ValueError as exc:
        raise AmendError(str(exc)) from exc
    for goal in profile.goals:
        if goal.priority is Priority.A and exception.mode == "off" and exception.covers(goal.date):
            raise AmendError(f"A goal '{goal.name}' ({goal.date}) is inside this off range")
    notes = [
        _issue(
            "pinned_in_range",
            f"{s.date} {s.name} stays: it is pinned by amendment {s.amendment_id} (undo it to "
            "free the day)",
            week_index(plan, s.date) + 1,
        )
        for week in plan.weeks
        for s in week.sessions
        if s.pinned and exception.covers(s.date)
    ]
    staged = plan.model_copy(update={"amendments": [*plan.amendments, amendment]})
    first = max(0, week_index(plan, max(start, today)))
    last = week_index(plan, end)

    def reason_of(day: dt.date) -> str | None:
        return "calendar_exception" if start <= day <= end else None

    new_plan, changes, issues = _apply(
        plan, staged, profile, today, scope, (first, last), reason_of, notes
    )
    return AmendResult(new_plan, amendment, changes, issues)


def undo_amendment(plan: Plan, profile: Profile, today: dt.date, amendment_id: str) -> AmendResult:
    """Remove an amendment and replan from today: the result is the plan a replan without
    that amendment gives. An amendment it had superseded becomes active again."""
    target = next((a for a in plan.amendments if a.id == amendment_id), None)
    if target is None:
        known = ", ".join(a.id for a in plan.amendments) or "none"
        raise AmendError(f"no amendment {amendment_id} in this plan (amendments: {known})")
    if target.kind is AmendmentKind.SESSION and target.date < today:
        raise AmendError(f"amendment {amendment_id} concerns {target.date}, a past day")
    effective = plan.effective_profile(profile)
    thresholds, _ = resolve_thresholds(effective, today)
    log: list[Amendment] = []
    restored: list[Session] = []
    for amendment in plan.amendments:
        if amendment.id == amendment_id:
            continue
        update: dict[str, object] = {}
        if amendment.replaces == amendment_id:
            update["replaces"] = target.replaces
        if amendment.id == target.replaces and target.active:
            update["superseded_by"] = None
            assert amendment.original is not None
            again, _ = amended_session(
                amendment.original,
                edit_of(amendment),
                effective,
                thresholds,
                _goal_of(plan, amendment.date),
                amendment.id,
                amendment.created,
            )
            restored += [again] if again is not None else []
        log.append(amendment.model_copy(update=update) if update else amendment)
    drop = [s for week in plan.weeks for s in week.sessions if s.amendment_id == amendment_id]
    staged = plan.model_copy(
        update={"weeks": _with_sessions(plan, drop, restored), "amendments": log}
    )
    dates = {target.date, target.move_to} | {s.date for s in drop + restored}
    if target.kind is AmendmentKind.UNAVAILABLE:
        days = (target.last - target.date).days + 1
        dates |= {target.date + dt.timedelta(days=offset) for offset in range(days)}

    def reason_of(day: dt.date) -> str | None:
        return "undone" if day in dates else None

    result = replan_plan(
        staged,
        profile,
        today,
        reason_of=reason_of,
        default_reason="rebalanced",
        baseline=plan,
    )
    return AmendResult(result.plan, None, result.changes, result.issues)


def swap_sport(
    plan: Plan, profile: Profile, today: dt.date, date: dt.date, *, force: bool = False
) -> AmendResult:
    """`plan swap`: the session of `date` done in the other sport, as an amendment with
    scope `session` (same conversion and checks as swap.swap_session, and the session is
    pinned so that a replan keeps it)."""
    session = next((s for week in plan.weeks for s in week.sessions if s.date == date), None)
    if session is None:
        raise NoSession(f"no session on {date}")
    edit = SessionEdit(sport=Sport.RUN if session.sport is Sport.BIKE else Sport.BIKE)
    return amend_session(plan, profile, today, date, edit, scope=Scope.SESSION, force=force)


def describe_amendment(amendment: Amendment) -> str:
    if amendment.kind is AmendmentKind.UNAVAILABLE:
        exception = amendment.as_exception()
        what = f"unavailable {exception.label()}"
    else:
        original = amendment.original
        name = f" {original.name}" if original is not None else ""
        what = f"{amendment.date}{name}: {edit_of(amendment).describe()}"
    status = "active" if amendment.active else f"superseded by {amendment.superseded_by}"
    reason = f" - {amendment.reason}" if amendment.reason else ""
    force = ", forced" if amendment.force else ""
    return (
        f"{amendment.id:<4} {what} (scope {amendment.scope.value}, made {amendment.created}, "
        f"{status}{force}){reason}"
    )


def amendment_lines(plan: Plan) -> list[str]:
    """The amendment log, one line per amendment."""
    if not plan.amendments:
        return ["no amendment"]
    return [describe_amendment(amendment) for amendment in plan.amendments]
