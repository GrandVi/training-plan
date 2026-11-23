"""Replanning a running plan: the past stays, the rest is rebuilt around what is fixed.

replan(plan, profile, today) rebuilds every week from the one that contains `today`:
- Sessions dated before today are frozen (done). Weeks that are over are kept as they are.
- The skeleton is build_skeleton(profile, first week start). If it no longer matches the
  weeks already started (for example a goal date was edited after the start), replanning
  stops with `skeleton_changed`: generate a new plan instead.
- The scheduler's progression state (volume level, rotations, ladders) is seeded from the
  kept weeks (Scheduler.seed), so a week is rebuilt exactly as it was first built when
  nothing changed.
- Pinned (amended) sessions, skipped days and the frozen sessions of the current week are
  given to Scheduler.build_week as WeekPins: the rest of each week is built around them.
- The plan id does not change. Sessions that did not change keep their external_id, so
  `push` only updates differences. The result lists every change with a reason code.

The plan's unavailable amendments are added to the profile's calendar exceptions
(Plan.effective_profile). profile.yaml is never written here.
"""

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from endurance_planner.load import run_impact_multiplier, week_weighted_load, weighted_load
from endurance_planner.models.enums import AmendmentKind, Severity, Sport
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Plan, Session, StrengthSession, Week
from endurance_planner.models.profile import Profile
from endurance_planner.periodization import PlanningError, WeekSkeleton, build_skeleton
from endurance_planner.planner import plan_warnings
from endurance_planner.rules import KEY_COMPLETED_SHARE, OVERREACH_FACTOR
from endurance_planner.scheduler import Scheduler, WeekPins
from endurance_planner.strength import StrengthPins, StrengthPlanner
from endurance_planner.templates import progress_rung
from endurance_planner.thresholds import resolve_thresholds
from endurance_planner.units import format_duration
from endurance_planner.validation import validate_plan

ReasonOf = Callable[[dt.date], str | None]


class ReplanError(PlanningError):
    """The plan cannot be replanned; `code` says why (e.g. skeleton_changed)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Change:
    """One day whose session (or strength session) changed; `code` is a key of
    rules.CHANGE_REASONS."""

    date: dt.date
    code: str
    before: Session | StrengthSession | None
    after: Session | StrengthSession | None

    @property
    def strength(self) -> bool:
        return isinstance(self.before or self.after, StrengthSession)

    def describe(self) -> str:
        none = "no strength" if self.strength else "rest"
        before = session_label(self.before, none)
        return f"{self.date} {self.code:<21} {before} -> " + session_label(self.after, none)


@dataclass(frozen=True)
class ReplanResult:
    plan: Plan
    changes: list[Change]
    issues: list[Issue]  # new warnings, and the errors this replan introduced

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]


def session_label(session: Session | StrengthSession | None, none: str = "rest") -> str:
    if session is None:
        return none
    pin = " (pinned)" if session.pinned else ""
    if isinstance(session, StrengthSession):
        done = " (done)" if session.actual else ""
        return f"Strength {session.minutes}m {session.name}{pin}{done}"
    if session.actual:
        return f"{session.sport.value} {format_duration(session.duration_s)} {session.name} (done)"
    kind = session.activity_type.value + (" indoor" if session.indoor else "")
    duration = format_duration(session.duration_s)
    return f"{kind} {duration} {session.name} [{session.slot.value}]{pin}"


def change_lines(changes: Sequence[Change]) -> list[str]:
    """Printable diff, one line per changed day."""
    if not changes:
        return ["no session changes"]
    return [f"  {change.describe()}" for change in changes]


def diff_plans(before: Plan, after: Plan, reason_of: ReasonOf, default: str) -> list[Change]:
    """Days whose session or strength session differs between two versions of a plan."""
    changes: list[Change] = []
    old: dict[dt.date, Session | StrengthSession]
    new: dict[dt.date, Session | StrengthSession]
    for field in ("sessions", "strength"):
        old = {s.date: s for week in before.weeks for s in getattr(week, field)}
        new = {s.date: s for week in after.weeks for s in getattr(week, field)}
        for date in sorted(old.keys() | new.keys()):
            if old.get(date) != new.get(date):
                code = reason_of(date) or default
                changes.append(Change(date, code, old.get(date), new.get(date)))
    return sorted(changes, key=lambda change: (change.date, change.strength))


def introduced_issues(before: Plan, after: Plan, profile: Profile) -> list[Issue]:
    """Validation errors of `after` that `before` does not already have (same rule, same
    week), and the warnings it adds."""
    old = validate_plan(before, profile)
    known_errors = {(i.code, i.week) for i in old if i.severity is Severity.ERROR}
    known_warnings = {str(i) for i in old if i.severity is Severity.WARNING}
    return [
        issue
        for issue in validate_plan(after, profile)
        if (issue.severity is Severity.ERROR and (issue.code, issue.week) not in known_errors)
        or (issue.severity is Severity.WARNING and str(issue) not in known_warnings)
    ]


def week_index(plan: Plan, date: dt.date) -> int:
    """Index of the plan week containing `date` (may be outside the plan)."""
    return (date - plan.weeks[0].start).days // 7


def _shape(item: Week | WeekSkeleton) -> tuple[object, ...]:
    if isinstance(item, Week):
        return (item.start, item.phase, item.recovery, item.goal, tuple(item.races))
    races = [goal.name for goal in item.minor_races]
    if item.race is not None:
        races.append(item.race.name)
    return (item.start, item.phase, item.recovery, item.goal.name, tuple(races))


def _lost(plan: Plan, start: dt.date, impact: float) -> tuple[float, float, float]:
    """Volume, weighted load and run time taken out of the week by its amendments, and by
    the days done below plan (plan adapt): the other days do not make them up."""
    lost_s = lost_load = lost_run_s = 0.0
    week = next((w for w in plan.weeks if w.start == start), None)
    if week is not None and week.planned_sessions:
        done = [s for s in week.sessions if s.actual]
        planned = week.planned_sessions

        def run_of(sessions: Sequence[Session]) -> float:
            return sum(s.duration_s for s in sessions if s.sport is Sport.RUN)

        lost_s += max(0.0, sum(s.duration_s for s in planned) - sum(s.duration_s for s in done))
        lost_load += max(
            0.0,
            sum(weighted_load(s, impact) for s in planned)
            - sum(weighted_load(s, impact) for s in done),
        )
        lost_run_s += max(0.0, run_of(planned) - run_of(done))
    results = {s.amendment_id: s for week in plan.weeks for s in week.sessions if s.amendment_id}
    for amendment in plan.active_amendments(AmendmentKind.SESSION):
        original = amendment.original
        if original is None or not start <= original.date < start + dt.timedelta(weeks=1):
            continue
        result = results.get(amendment.id)
        run = original.duration_s if original.sport is Sport.RUN else 0
        if result is None:
            lost_s += original.duration_s
            lost_load += weighted_load(original, impact)
            lost_run_s += run
            continue
        result_run = result.duration_s if result.sport is Sport.RUN else 0
        lost_s += max(0, original.duration_s - result.duration_s)
        lost_load += max(0.0, weighted_load(original, impact) - weighted_load(result, impact))
        lost_run_s += max(0, run - result_run)
    return lost_s, lost_load, lost_run_s


def _originals(plan: Plan, start: dt.date) -> list[Session]:
    """Sessions of the week starting on `start` as planned before its active amendments."""
    end = start + dt.timedelta(days=6)
    return [
        a.original
        for a in plan.active_amendments(AmendmentKind.SESSION)
        if a.original is not None and start <= a.original.date <= end
    ]


def _pins(plan: Plan, sk: WeekSkeleton, today: dt.date, impact: float) -> WeekPins:
    end = sk.start + dt.timedelta(days=6)
    sessions = [s for week in plan.weeks for s in week.sessions]
    fixed = [s for s in sessions if sk.start <= s.date <= end and (s.date < today or s.pinned)]
    skipped = [
        a.original
        for a in plan.active_amendments(AmendmentKind.SESSION)
        if a.skip and a.original is not None and sk.start <= a.original.date <= end
    ]
    next_end = end + dt.timedelta(days=7)
    ahead = [s for s in sessions if s.pinned and end < s.date <= next_end]
    lost_s, lost_load, lost_run_s = _lost(plan, sk.start, impact)
    behind = _originals(plan, sk.start - dt.timedelta(weeks=1))
    strength_days = [
        s.date
        for s in _fixed_strength(plan, today)
        if sk.start - dt.timedelta(days=1) <= s.date <= end
    ]
    return WeekPins(
        sessions=tuple(sorted(fixed, key=lambda s: s.date)),
        skipped=tuple(skipped),
        not_before=today if sk.start < today else None,
        lost_s=lost_s,
        lost_load=lost_load,
        lost_run_s=lost_run_s,
        ahead=tuple(ahead),
        behind=tuple(behind),
        strength_days=tuple(strength_days),
    )


def _fixed_strength(plan: Plan, today: dt.date) -> list[StrengthSession]:
    """Strength sessions a replan keeps: done (before today) or amended (pinned)."""
    return [s for week in plan.weeks for s in week.strength if s.date < today or s.pinned]


def strength_pins(plan: Plan, index: int, today: dt.date) -> StrengthPins:
    """Fixed strength content of plan week `index` for a replan."""
    if index >= len(plan.weeks):
        return StrengthPins()
    week = plan.weeks[index]
    end = week.start + dt.timedelta(days=6)
    return StrengthPins(
        sessions=tuple(s for s in _fixed_strength(plan, today) if week.start <= s.date <= end),
        skipped=tuple(_skipped_strength(plan, week.start, end)),
        not_before=today if week.start < today else None,
    )


def _missed_strength(plan: Plan) -> set[dt.date]:
    """Planned strength sessions that were not done (plan adapt): their ladder holds."""
    return {
        planned.date
        for week in plan.weeks
        for planned in week.planned_strength
        if planned.date not in {s.date for s in week.strength if s.actual}
    }


def _skipped_strength(plan: Plan, start: dt.date, end: dt.date) -> list[dt.date]:
    """Dates of strength sessions skipped by an amendment (not replaced: no catch-up)."""
    return [
        a.date
        for a in plan.active_amendments(AmendmentKind.STRENGTH)
        if a.skip and start <= a.date <= end
    ]


def _keep_planned(plan: Plan, week: Week) -> Week:
    """A rebuilt week keeps the planned sessions of its days already done (plan adapt)."""
    source = next((w for w in plan.weeks if w.start == week.start), None)
    if source is None or not (source.planned_sessions or source.planned_strength):
        return week
    return week.model_copy(
        update={
            "planned_sessions": source.planned_sessions,
            "planned_strength": source.planned_strength,
        }
    )


def _agrees(planned: Sequence[Session], done: Sequence[Session]) -> bool:
    """The days done match the plan: the same sessions, or completed activities (plan adapt)
    of the same day, sport, slot and key session, between 80% and 110% of the planned time."""
    if len(planned) != len(done):
        return False
    for plan_s, done_s in zip(planned, done, strict=True):
        if plan_s == done_s:
            continue
        if not done_s.actual or (plan_s.date, plan_s.sport, plan_s.slot) != (
            done_s.date,
            done_s.sport,
            done_s.slot,
        ):
            return False
        if done_s.template_id != plan_s.template_id or done_s.rung != progress_rung(plan_s):
            return False
        share = done_s.duration_s / plan_s.duration_s
        if not KEY_COMPLETED_SHARE <= share <= OVERREACH_FACTOR:
            return False
    return True


def _build_unamended(
    scheduler: Scheduler, sk: WeekSkeleton, weeks: list[Week], pins: WeekPins, today: dt.date
) -> Week:
    """A week without amendments. If it has started, it is first built as at generation time
    and kept when it agrees with what was done; otherwise it is built around the past."""
    around = WeekPins(ahead=pins.ahead, behind=pins.behind, strength_days=pins.strength_days)
    only_ahead = around if pins.ahead or pins.behind or pins.strength_days else None
    if not pins.sessions and pins.not_before is None:
        return scheduler.build_week(sk, weeks, only_ahead)
    state = scheduler.snapshot()
    trial = scheduler.build_week(sk, weeks, only_ahead)
    if _agrees([s for s in trial.sessions if s.date < today], list(pins.sessions)):
        later = [s for s in trial.sessions if s.date >= today]
        return trial.model_copy(update={"sessions": [*pins.sessions, *later]})
    scheduler.restore(state)
    return scheduler.build_week(sk, weeks, pins)


def _build(
    scheduler: Scheduler,
    sk: WeekSkeleton,
    weeks: list[Week],
    pins: WeekPins,
    originals: Sequence[Session],
    today: dt.date,
) -> Week:
    """One week rebuilt around its pins.

    With amendments, the week is first built without them (the reference). The other
    sessions may then have, together, at most the volume and load they have in the
    reference: what the amended sessions lose is never made up (no catch-up).
    """
    if not originals:
        return _build_unamended(scheduler, sk, weeks, pins, today)
    done = tuple(s for s in pins.sessions if not s.pinned)
    state = scheduler.snapshot()
    plain = WeekPins(
        sessions=done,
        not_before=pins.not_before,
        ahead=pins.ahead,
        behind=pins.behind,
        strength_days=pins.strength_days,
    )
    reference = _build_unamended(scheduler, sk, weeks, plain, today)
    scheduler.restore(state)
    kept = [*done, *originals]
    impact = scheduler.impact
    pins = replace(
        pins,
        others_max_s=max(0.0, reference.duration_s - sum(s.duration_s for s in kept)),
        others_max_load=max(
            0.0,
            week_weighted_load(reference, impact) - sum(weighted_load(s, impact) for s in kept),
        ),
    )
    return scheduler.build_week(sk, weeks, pins)


def replan(
    plan: Plan, profile: Profile, today: dt.date, history: Sequence[Week] | None = None
) -> Plan:
    """The plan rebuilt from `today` (see the module docstring)."""
    return replan_plan(plan, profile, today, history).plan


def replan_plan(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    history: Sequence[Week] | None = None,
    *,
    first_week: int | None = None,
    last_week: int | None = None,
    reason_of: ReasonOf | None = None,
    default_reason: str = "replanned",
    baseline: Plan | None = None,
    strength_held: Sequence[dt.date] = (),
) -> ReplanResult:
    """Rebuild weeks first_week..last_week (indexes; default: from the current week to the
    end) and return the plan with its changes and issues.

    history: weeks actually done, replacing the plan's weeks before first_week (hook for
    adaptation from completed activities). baseline: the plan the changes are measured
    against (default: `plan`). Errors are not raised: the caller decides (see
    ReplanResult.errors, which only lists errors the baseline does not already have).
    strength_held: dates of strength sessions not done (plan adapt): their ladder holds.
    """
    baseline = baseline or plan
    effective = plan.effective_profile(profile)
    thresholds, threshold_issues = resolve_thresholds(effective, today)
    start = plan.weeks[0].start
    skeleton, skeleton_issues = build_skeleton(effective, start)
    current = max(0, week_index(plan, today))
    first = current if first_week is None else first_week
    last = len(skeleton) - 1 if last_week is None else min(last_week, len(skeleton) - 1)

    started = first + (first < len(plan.weeks) and plan.weeks[first].start < today)
    for index, week in enumerate(plan.weeks[:started]):
        if index >= len(skeleton) or _shape(skeleton[index]) != _shape(week):
            raise ReplanError(
                "skeleton_changed",
                f"week {index + 1} ({week.start}) is already planned as {week.phase.value}"
                f"{' (recovery)' if week.recovery else ''} for '{week.goal}', but the profile "
                "now gives another structure (goal dates or mesocycle pattern changed). "
                "Generate a new plan from next Monday instead.",
            )
    kept = list(history) if history is not None else plan.weeks[:first]
    if [week.start for week in kept] != [week.start for week in plan.weeks[:first]]:
        raise ValueError("history must give the weeks before the first rebuilt week")

    scheduler = Scheduler(effective, thresholds)
    scheduler.seed(skeleton[:first], kept)
    impact = run_impact_multiplier(effective)
    weeks = list(kept)
    for sk in skeleton[first : last + 1]:
        pins = _pins(plan, sk, today, impact)
        built = _build(scheduler, sk, weeks, pins, _originals(plan, sk.start), today)
        weeks.append(_keep_planned(plan, built))
    weeks += plan.weeks[last + 1 :] if last_week is not None else []
    # Strength goes in after the endurance sessions. The week before the first rebuilt one is
    # redone too when it is not over: its last days depend on the rebuilt week's first ones.
    low = (
        first - 1 if first > 0 and plan.weeks[first - 1].start + dt.timedelta(6) >= today else first
    )
    planner = StrengthPlanner(effective, profile.goals)
    planner.held = (
        set(_skipped_strength(plan, plan.weeks[0].start, plan.weeks[-1].start + dt.timedelta(6)))
        | set(strength_held)
        | _missed_strength(plan)
    )
    planner.seed(weeks[:low])
    pins_of = {index: strength_pins(plan, index, today) for index in range(low, last + 1)}
    weeks = planner.place(weeks, low, last, pins_of)

    issues: list[Issue] = []
    placed = {s.date for week in weeks for s in week.sessions if s.pinned}
    for week in plan.weeks:
        for session in week.sessions:
            if session.pinned and session.date >= today and session.date not in placed:
                issues.append(
                    Issue(
                        severity=Severity.WARNING,
                        code="pinned_session_dropped",
                        message=f"{session.date} {session.name} (amendment "
                        f"{session.amendment_id}) is no longer inside the plan",
                    )
                )
    warnings = (
        threshold_issues
        + skeleton_issues
        + plan_warnings(effective, start, weeks, scheduler)
        + planner.issues()
    )
    replanned = plan.model_copy(
        update={
            "weeks": weeks,
            "goals": profile.goals,
            "athlete": profile.athlete.name,
            "warnings": [],
        }
    )
    validation = validate_plan(replanned, profile)
    replanned.warnings = warnings + [i for i in validation if i.severity is Severity.WARNING]
    issues += introduced_issues(baseline, replanned, profile)

    def reason(date: dt.date) -> str | None:
        given = reason_of(date) if reason_of is not None else None
        if given is None and effective.availability.exception_on(date) is not None:
            return "calendar_exception"
        return given

    changes = diff_plans(baseline, replanned, reason, default_reason)
    return ReplanResult(replanned, changes, issues)
