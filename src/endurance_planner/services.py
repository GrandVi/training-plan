"""Use cases of the planner, shared by the CLI, the HTTP API and future front-ends.

Pure: no file, network, environment or clock access. Every function takes the objects it
works on (profile, plan, `today`, an intervals.icu client) and returns pydantic models or
JSON-compatible data. They call the planning library; they do not re-implement it.

Mutations (generate, amend, declare_unavailable, replan, undo_amendment, swap, retarget)
return an `Outcome`: the plan to store (None for a dry run or a refused change) and a
`MutationResult` (changes with reason codes, issues, new revision). They take
`expected_revision` and raise RevisionConflict when the plan has another revision.

`PlannerService` runs the same use cases on a `Storage` (load, lock, save), which is what
the HTTP API uses; `storage.FileStorage` is the file implementation.
"""

import datetime as dt
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from endurance_planner import amend as amend_lib
from endurance_planner import planner, validation
from endurance_planner import replan as replan_lib
from endurance_planner.export import intervals_icu
from endurance_planner.export.markdown import (
    fitness_csv,
    render_markdown,
    week_exceptions,
    week_row,
    weekly_csv,
)
from endurance_planner.load import run_impact_multiplier, strength_tss
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
from endurance_planner.models.plan import (
    Amendment,
    Exercise,
    Plan,
    Repeat,
    Session,
    Step,
    StrengthSession,
)
from endurance_planner.models.profile import Profile
from endurance_planner.profile_tools import zone_report
from endurance_planner.rules import DEFAULT_SCOPE
from endurance_planner.thresholds import resolve_thresholds
from endurance_planner.units import format_clock
from endurance_planner.zones import zone_set, zone_tables

PlanFormat = Literal["md", "csv", "fitness-csv"]
FORMATS: tuple[str, ...] = ("md", "csv", "fitness-csv")


# ---------------------------------------------------------------------- errors
class RevisionConflict(Exception):
    """The plan was changed elsewhere: the change was based on another revision."""

    def __init__(self, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            f"the plan changed elsewhere (revision {current}, this change was based on "
            f"revision {expected}): reload it and try again"
        )


class PlanNotFound(LookupError):
    """No plan yet (generate one first), or no plan with that id."""


class UnknownWeek(LookupError):
    """The plan has no week with that number."""


class UnknownFormat(ValueError):
    """The output format is not one of FORMATS."""


class StorageBusy(Exception):
    """Another writer holds the storage lock."""


# ---------------------------------------------------------------------- views
class SessionView(BaseModel):
    """A session as listed in a week or a diff."""

    date: dt.date
    weekday: str  # "Mon"
    sport: Sport
    activity_type: ActivityType
    slot: Slot
    intent: Intent
    hard: bool
    name: str
    duration_s: int
    tss: float
    indoor: bool
    technical: bool
    pinned: bool
    amendment_id: str | None

    @classmethod
    def of(cls, session: Session) -> "SessionView":
        return cls(
            date=session.date,
            weekday=f"{session.date:%a}",
            sport=session.sport,
            activity_type=session.activity_type,
            slot=session.slot,
            intent=session.intent,
            hard=session.intent.hard,
            name=session.name,
            duration_s=session.duration_s,
            tss=session.tss,
            indoor=session.indoor,
            technical=session.technical,
            pinned=session.pinned,
            amendment_id=session.amendment_id,
        )


class StrengthView(BaseModel):
    """A strength session as listed in a week or a diff."""

    date: dt.date
    weekday: str
    name: str
    minutes: int
    tss: float  # flat estimate (load.STRENGTH_TSS_PER_HOUR)
    phase: Phase
    template_id: str
    variant: str
    equipment: list[str]
    pinned: bool
    amendment_id: str | None

    @classmethod
    def of(cls, session: StrengthSession) -> "StrengthView":
        return cls(
            date=session.date,
            weekday=f"{session.date:%a}",
            name=session.name,
            minutes=session.minutes,
            tss=strength_tss(session.minutes),
            phase=session.phase,
            template_id=session.template_id,
            variant=session.variant,
            equipment=[item.value for item in session.equipment_used],
            pinned=session.pinned,
            amendment_id=session.amendment_id,
        )


class StrengthDetail(StrengthView):
    rationale: str
    exercises: list[Exercise]
    lines: list[str]  # one per exercise, as in the description pushed to intervals.icu


class StepLine(BaseModel):
    """One line of a session's structure. `repeat` is set on the header of a repeated block;
    the lines of the block follow with depth + 1."""

    depth: int
    repeat: int | None = None
    kind: StepKind | None = None
    duration_s: int | None = None
    distance_m: int | None = None
    target: str = ""  # relative (e.g. "95-100% FTP")
    absolute: str = ""  # e.g. "247-260 W, 155-163 bpm"
    note: str = ""


class SessionDetail(SessionView):
    rationale: str
    notes: str
    template_id: str | None
    steps: list[StepLine]


class WeekView(BaseModel):
    index: int  # 1-based
    weeks: int  # number of weeks in the plan
    start: dt.date
    end: dt.date
    phase: Phase
    recovery: bool
    goal: str
    races: list[str]
    duration_s: int
    tss: float
    load: float  # impact-weighted load (used for the ramp caps)
    low_pct: int
    high_pct: int
    exceptions: list[str]  # calendar exceptions touching the week
    sessions: list[SessionDetail]


class WeekSummary(BaseModel):
    index: int
    start: dt.date
    phase: Phase
    recovery: bool
    hours: float
    tss: float
    load: float
    key_sessions: list[str]
    races: list[str]
    exception: bool


class GoalView(BaseModel):
    name: str
    date: dt.date
    type: str
    priority: Priority


class PlanOverview(BaseModel):
    id: str
    revision: int
    created: dt.date
    athlete: str
    start: dt.date
    end: dt.date
    current_week: int | None  # week containing today (1-based), None outside the plan
    goals: list[GoalView]
    weeks: list[WeekSummary]
    warnings: list[Issue]
    amendments: int


class ThresholdView(BaseModel):
    name: str
    value: str
    estimated: bool


class ZoneLine(BaseModel):
    name: str
    label: str


class ProfileSummary(BaseModel):
    """What the plan is built from, read-only (the profile is edited by hand)."""

    athlete: str
    experience: str
    thresholds: list[ThresholdView]
    zones: dict[str, list[ZoneLine]]
    warnings: list[Issue]
    goals: list[GoalView]
    days_per_week: int
    weekly_hours_budget: float
    exceptions: list[str]


class ChangeView(BaseModel):
    date: dt.date
    code: str  # key of rules.CHANGE_REASONS
    kind: Literal["session", "strength"] = "session"
    before: SessionView | None = None
    after: SessionView | None = None
    strength_before: StrengthView | None = None  # kind "strength"
    strength_after: StrengthView | None = None
    line: str  # the line printed by the CLI

    @classmethod
    def of(cls, change: replan_lib.Change) -> "ChangeView":
        def view(item: Session | StrengthSession | None) -> SessionView | None:
            return SessionView.of(item) if isinstance(item, Session) else None

        def strength(item: Session | StrengthSession | None) -> StrengthView | None:
            return StrengthView.of(item) if isinstance(item, StrengthSession) else None

        return cls(
            date=change.date,
            code=change.code,
            kind="strength" if change.strength else "session",
            before=view(change.before),
            after=view(change.after),
            strength_before=strength(change.before),
            strength_after=strength(change.after),
            line=change.describe(),
        )


class MutationResult(BaseModel):
    plan_id: str
    revision: int  # revision of the stored plan after the call
    dry_run: bool
    applied: bool  # the plan was (or, in a dry run, would be) stored
    refused: bool  # errors and no force: nothing stored
    changed: bool  # the stored plan differs from the previous one
    message: str = ""  # one-line summary ("amendment a1: 40 min (scope rest)")
    amendment: Amendment | None = None
    changes: list[ChangeView]
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]


@dataclass(frozen=True)
class Outcome:
    """Result of a mutation: `stored` is the plan to save (None: save nothing) and
    `candidate` the plan computed (shown by previews)."""

    stored: Plan | None
    candidate: Plan
    result: MutationResult


class ValidationReport(BaseModel):
    issues: list[Issue]
    errors: int
    warnings: int


class AmendmentView(BaseModel):
    id: str
    kind: AmendmentKind
    created: dt.date
    date: dt.date
    to: dt.date | None
    scope: Scope
    active: bool
    superseded_by: str | None
    reason: str
    force: bool
    line: str  # as printed by `plan amendments`


class PushReport(BaseModel):
    dry_run: bool
    start: dt.date
    end: dt.date | None
    events: int  # workouts in the window
    payloads: list[dict[str, Any]] = Field(default_factory=list)  # dry run: what would be sent
    upserted: int = 0
    pruned: int = 0
    unchanged: int = 0
    threshold_diffs: list[str] = Field(default_factory=list)  # profile vs intervals.icu


# ---------------------------------------------------------------------- requests
class AmendRequest(BaseModel):
    """Change one session (see amend.py). Flags combine, except `skip`."""

    model_config = ConfigDict(extra="forbid")

    date: dt.date
    minutes: int | None = Field(None, gt=0)
    sport: Sport | None = None
    activity: BikeActivity | None = None
    intent: Intent | None = None
    skip: bool = False
    move_to: dt.date | None = None
    scope: Scope = DEFAULT_SCOPE
    reason: str = ""

    def edit(self) -> amend_lib.SessionEdit:
        return amend_lib.SessionEdit(
            minutes=self.minutes,
            sport=self.sport,
            activity=self.activity,
            intent=self.intent,
            skip=self.skip,
            move_to=self.move_to,
        )


class UnavailableRequest(BaseModel):
    """A date range with no training (no `minutes`) or limited minutes per sport."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start: dt.date = Field(alias="from")
    end: dt.date | None = Field(None, alias="to")
    minutes: dict[Sport, int] | None = None
    activity: BikeActivity | None = None
    scope: Scope = DEFAULT_SCOPE
    reason: str = ""


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    dry_run: bool = False
    start: dt.date | None = Field(None, alias="from")  # default: today
    end: dt.date | None = Field(None, alias="to")
    absolute_targets: bool = False
    prune: bool = True
    only_changed: bool = True


# ---------------------------------------------------------------------- helpers
def check_revision(plan: Plan | None, expected: int | None) -> None:
    """Raise RevisionConflict when `expected` is given and differs (no plan = revision 0)."""
    current = plan.revision if plan is not None else 0
    if expected is not None and expected != current:
        raise RevisionConflict(expected, current)


def _finish(
    old: Plan,
    new: Plan,
    *,
    changes: Sequence[replan_lib.Change],
    issues: Sequence[Issue],
    dry_run: bool,
    force: bool,
    message: str = "",
    amendment: Amendment | None = None,
) -> Outcome:
    errors = any(issue.severity is Severity.ERROR for issue in issues)
    refused = errors and not force
    applied = not refused
    candidate = new.model_copy(update={"revision": old.revision})
    changed = candidate != old
    if changed:
        candidate = candidate.model_copy(update={"revision": old.revision + 1})
    keep = applied and not dry_run
    result = MutationResult(
        plan_id=old.id,
        revision=candidate.revision if keep else old.revision,
        dry_run=dry_run,
        applied=applied,
        refused=refused,
        changed=changed,
        message=message,
        amendment=amendment,
        changes=[ChangeView.of(change) for change in changes],
        issues=list(issues),
    )
    return Outcome(candidate if keep else None, candidate, result)


def _session_lines(blocks: Sequence[Step | Repeat], depth: int = 0) -> list[StepLine]:
    lines: list[StepLine] = []
    for block in blocks:
        if isinstance(block, Repeat):
            lines.append(StepLine(depth=depth, repeat=block.count))
            lines += _session_lines(block.steps, depth + 1)
            continue
        lines.append(
            StepLine(
                depth=depth,
                kind=block.kind,
                duration_s=block.duration_s,
                distance_m=block.distance_m,
                target=block.target.label(),
                absolute=block.resolved.label() if block.resolved else "",
                note=block.note,
            )
        )
    return lines


def _goals(plan_or_profile: Plan | Profile) -> list[GoalView]:
    return [
        GoalView(name=g.name, date=g.date, type=g.type.value, priority=g.priority)
        for g in plan_or_profile.goals
    ]


# ---------------------------------------------------------------------- reading
def profile_summary(profile: Profile, today: dt.date) -> ProfileSummary:
    """Resolved thresholds, zone tables and warnings (no secret is part of the profile)."""
    thresholds, issues = resolve_thresholds(profile, today)
    estimated = set(thresholds.estimated)
    values: list[ThresholdView] = []
    for field, label, unit in (
        ("ftp_w", "Bike FTP", "W"),
        ("bike_lthr_bpm", "Bike LTHR", "bpm"),
        ("bike_max_hr_bpm", "Bike max HR", "bpm"),
        ("threshold_pace_s", "Run threshold pace", "/km"),
        ("run_lthr_bpm", "Run LTHR", "bpm"),
        ("run_max_hr_bpm", "Run max HR", "bpm"),
    ):
        value = getattr(thresholds, field)
        if value is None:
            continue
        if field == "threshold_pace_s":
            text = f"{format_clock(value)} {unit}"
        else:
            text = f"{value:.0f} {unit}"
        values.append(ThresholdView(name=label, value=text, estimated=field in estimated))
    tables = zone_tables(thresholds, zone_set(profile.thresholds.zone_models))
    availability = profile.availability
    return ProfileSummary(
        athlete=profile.athlete.name,
        experience=profile.athlete.experience.value,
        thresholds=values,
        zones={
            title: [ZoneLine(name=row.name, label=row.label) for row in rows]
            for title, rows in tables.items()
        },
        warnings=issues,
        goals=_goals(profile),
        days_per_week=availability.days_per_week,
        weekly_hours_budget=availability.weekly_hours_budget,
        exceptions=[exception.label() for exception in availability.exceptions],
    )


def profile_text(profile: Profile, today: dt.date) -> str:
    """`profile validate` output: warnings and zone tables as text."""
    return zone_report(profile, today)


def plan_overview(plan: Plan, profile: Profile, today: dt.date) -> PlanOverview:
    effective = plan.effective_profile(profile)
    impact = run_impact_multiplier(effective)
    zones = zone_set(effective.thresholds.zone_models)
    current = replan_lib.week_index(plan, today)
    weeks = []
    for week in plan.weeks:
        row = week_row(week, impact, zones)
        weeks.append(
            WeekSummary(
                index=week.index,
                start=week.start,
                phase=week.phase,
                recovery=week.recovery,
                hours=float(row["hours"]),
                tss=week.tss,
                load=float(row["weighted_load"]),
                key_sessions=[s.name for s in week.sessions if s.intent.hard],
                races=week.races,
                exception=bool(week_exceptions(week, effective)),
            )
        )
    return PlanOverview(
        id=plan.id,
        revision=plan.revision,
        created=plan.created,
        athlete=plan.athlete,
        start=plan.weeks[0].start,
        end=plan.weeks[-1].start + dt.timedelta(days=6),
        current_week=current + 1 if 0 <= current < len(plan.weeks) else None,
        goals=_goals(plan),
        weeks=weeks,
        warnings=plan.warnings,
        amendments=len(plan.amendments),
    )


def show_week(plan: Plan, profile: Profile, index: int) -> WeekView:
    """Week `index` (1-based) with every session's steps in absolute units."""
    if not 1 <= index <= len(plan.weeks):
        raise UnknownWeek(f"no week {index}: the plan has weeks 1 to {len(plan.weeks)}")
    week = plan.weeks[index - 1]
    effective = plan.effective_profile(profile)
    impact = run_impact_multiplier(effective)
    row = week_row(week, impact, zone_set(effective.thresholds.zone_models))
    return WeekView(
        index=week.index,
        weeks=len(plan.weeks),
        start=week.start,
        end=week.start + dt.timedelta(days=6),
        phase=week.phase,
        recovery=week.recovery,
        goal=week.goal,
        races=week.races,
        duration_s=week.duration_s,
        tss=week.tss,
        load=float(row["weighted_load"]),
        low_pct=int(row["low_pct"]),
        high_pct=int(row["high_pct"]),
        exceptions=[e.label() for e in week_exceptions(week, effective)],
        sessions=[_detail(session) for session in week.sessions],
    )


def _detail(session: Session) -> SessionDetail:
    return SessionDetail(
        **SessionView.of(session).model_dump(),
        rationale=session.rationale,
        notes=session.notes,
        template_id=session.template_id,
        steps=_session_lines(session.steps),
    )


def week_of(plan: Plan, date: dt.date) -> int:
    """1-based index of the week containing `date` (UnknownWeek outside the plan)."""
    index = replan_lib.week_index(plan, date) + 1
    if not 1 <= index <= len(plan.weeks):
        raise UnknownWeek(f"{date} is outside the plan ({plan.weeks[0].start} onwards)")
    return index


def render_plan(
    plan: Plan,
    profile: Profile,
    fmt: str = "md",
    weeks: Sequence[int] | None = None,
    details: bool = True,
) -> str:
    """`plan show`: Markdown, weekly CSV or daily CTL/ATL/TSB CSV."""
    if fmt == "md":
        return render_markdown(plan, profile, weeks=weeks, details=details)
    if fmt == "csv":
        return weekly_csv(plan, profile)
    if fmt == "fitness-csv":
        return fitness_csv(plan, profile)
    raise UnknownFormat(f"unknown format '{fmt}' ({', '.join(FORMATS)})")


def validate_plan(plan: Plan, profile: Profile, today: dt.date | None = None) -> ValidationReport:
    """Every rule; `today` defaults to the plan's creation date."""
    issues = validation.validate_plan(plan, profile, today)
    errors = sum(issue.severity is Severity.ERROR for issue in issues)
    return ValidationReport(issues=issues, errors=errors, warnings=len(issues) - errors)


def list_amendments(plan: Plan) -> list[AmendmentView]:
    return [
        AmendmentView(
            id=a.id,
            kind=a.kind,
            created=a.created,
            date=a.date,
            to=a.to,
            scope=a.scope,
            active=a.active,
            superseded_by=a.superseded_by,
            reason=a.reason,
            force=a.force,
            line=amend_lib.describe_amendment(a),
        )
        for a in plan.amendments
    ]


def change_lines(result: MutationResult) -> list[str]:
    """The diff as printed by the CLI."""
    if not result.changes:
        return ["no session changes"]
    return [f"  {change.line}" for change in result.changes]


# ---------------------------------------------------------------------- mutations
def generate_plan(
    profile: Profile,
    today: dt.date,
    start: dt.date | None = None,
    *,
    current: Plan | None = None,
    expected_revision: int | None = None,
    dry_run: bool = False,
) -> Outcome:
    """A new plan from the profile. Replacing `current`, its revision goes on (+1)."""
    check_revision(current, expected_revision)
    plan = planner.generate_plan(profile, today, start)
    message = f"{len(plan.weeks)} weeks from {plan.weeks[0].start}"
    result = MutationResult(
        plan_id=plan.id,
        revision=0,
        dry_run=dry_run,
        applied=True,
        refused=False,
        changed=True,
        message=message,
        changes=[],
        issues=plan.warnings,
    )
    if current is None:
        return Outcome(None if dry_run else plan, plan, result)
    changes = replan_lib.diff_plans(current, plan, lambda day: None, "regenerated")
    outcome = _finish(
        current, plan, changes=changes, issues=plan.warnings, dry_run=dry_run, force=False
    )
    outcome.result.message = message
    outcome.result.plan_id = plan.id
    return outcome


def amend(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    request: AmendRequest,
    *,
    expected_revision: int | None,
    dry_run: bool = False,
    force: bool = False,
) -> Outcome:
    """Edit, skip or move one session (amend.amend_session) and adapt the plan."""
    check_revision(plan, expected_revision)
    edit = request.edit()
    result = amend_lib.amend_session(
        plan,
        profile,
        today,
        request.date,
        edit,
        scope=request.scope,
        reason=request.reason,
        force=force,
    )
    message = ""
    if result.amendment is not None:
        message = f"amendment {result.amendment.id}: {edit.describe()} (scope {request.scope})"
    return _finish(
        plan,
        result.plan,
        changes=result.changes,
        issues=result.issues,
        dry_run=dry_run,
        force=force,
        message=message,
        amendment=result.amendment,
    )


def swap(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    date: dt.date,
    *,
    expected_revision: int | None,
    force: bool = False,
) -> Outcome:
    """`plan swap`: the session in the other sport, as an amendment of scope `session`."""
    check_revision(plan, expected_revision)
    result = amend_lib.swap_sport(plan, profile, today, date, force=force)
    session = next((s for w in result.plan.weeks for s in w.sessions if s.date == date), None)
    message = f"{date}: now a {session.sport.value} session, {session.name}" if session else ""
    return _finish(
        plan,
        result.plan,
        changes=result.changes,
        issues=result.issues,
        dry_run=False,
        force=force,
        message=message,
        amendment=result.amendment,
    )


def declare_unavailable(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    request: UnavailableRequest,
    *,
    expected_revision: int | None,
    dry_run: bool = False,
    force: bool = False,
) -> Outcome:
    """A date range unavailable or limited, kept in the plan (amend.add_unavailable)."""
    check_revision(plan, expected_revision)
    result = amend_lib.add_unavailable(
        plan,
        profile,
        today,
        request.start,
        request.end,
        request.minutes,
        request.activity,
        scope=request.scope,
        reason=request.reason,
        force=force,
    )
    message = ""
    if result.amendment is not None:
        label = result.amendment.as_exception().label()
        message = f"amendment {result.amendment.id}: unavailable {label}"
    return _finish(
        plan,
        result.plan,
        changes=result.changes,
        issues=result.issues,
        dry_run=dry_run,
        force=force,
        message=message,
        amendment=result.amendment,
    )


def replan(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    *,
    expected_revision: int | None,
    dry_run: bool = False,
    force: bool = False,
) -> Outcome:
    """Rebuild from today after a profile edit (replan.replan_plan)."""
    check_revision(plan, expected_revision)
    result = replan_lib.replan_plan(plan, profile, today)
    return _finish(
        plan,
        result.plan,
        changes=result.changes,
        issues=result.issues,
        dry_run=dry_run,
        force=force,
    )


def undo_amendment(
    plan: Plan,
    profile: Profile,
    today: dt.date,
    amendment_id: str,
    *,
    expected_revision: int | None,
    dry_run: bool = False,
    force: bool = False,
) -> Outcome:
    """Remove an amendment and replan (amend.undo_amendment)."""
    check_revision(plan, expected_revision)
    result = amend_lib.undo_amendment(plan, profile, today, amendment_id)
    return _finish(
        plan,
        result.plan,
        changes=result.changes,
        issues=result.issues,
        dry_run=dry_run,
        force=force,
        message=f"amendment {amendment_id} undone",
    )


def retarget(
    plan: Plan, profile: Profile, today: dt.date, *, expected_revision: int | None
) -> Outcome:
    """Absolute targets recomputed from the current thresholds (planner.retarget_plan)."""
    check_revision(plan, expected_revision)
    updated, issues = planner.retarget_plan(plan, profile, today)
    warnings = [issue for issue in issues if issue.severity is not Severity.ERROR]
    return _finish(plan, updated, changes=[], issues=warnings, dry_run=False, force=True)


# ---------------------------------------------------------------------- intervals.icu
def push_plan(
    plan: Plan,
    profile: Profile | None,
    today: dt.date,
    request: PushRequest,
    client: intervals_icu.IntervalsClient | None = None,
) -> PushReport:
    """Push the plan's workouts to intervals.icu. A dry run sends nothing and needs no
    client: it returns the payloads."""
    start = request.start or today
    payloads = intervals_icu.plan_events(plan, start, request.end, request.absolute_targets)
    report = PushReport(dry_run=request.dry_run, start=start, end=request.end, events=len(payloads))
    if request.dry_run:
        report.payloads = payloads
        return report
    if client is None or profile is None:
        raise ValueError("pushing needs the profile and an intervals.icu client")
    local, _ = resolve_thresholds(profile, today)
    remote = intervals_icu.remote_thresholds(client.athlete())
    report.threshold_diffs = [diff.label for diff in intervals_icu.threshold_diffs(local, remote)]
    result = intervals_icu.push_plan(
        client,
        plan,
        start=start,
        end=request.end,
        absolute=request.absolute_targets,
        prune=request.prune,
        only_changed=request.only_changed,
    )
    report.upserted, report.pruned, report.unchanged = (
        result.upserted,
        result.pruned,
        result.unchanged,
    )
    return report


# ---------------------------------------------------------------------- storage
class Storage(Protocol):
    """Where the profile and the plans live (see storage.FileStorage)."""

    def load_profile(self) -> Profile: ...

    def save_profile(self, profile: Profile) -> None: ...

    def load_plan(self, plan_id: str | None = None) -> Plan:
        """The plan with this id, or the current plan; PlanNotFound if there is none."""
        ...

    def save_plan(self, plan: Plan) -> None:
        """Store the plan and make it the current one."""
        ...

    def list_plan_ids(self) -> list[str]: ...

    def lock(self) -> AbstractContextManager[None]:
        """Exclusive lock for a read-modify-write; StorageBusy if it cannot be taken."""
        ...


Mutation = Callable[[Plan, Profile], Outcome]


class PlannerService:
    """The use cases on a Storage: reads, and mutations under the storage lock (load,
    check the revision, compute, save). Never writes the profile."""

    def __init__(
        self,
        storage: Storage,
        client_factory: Callable[[], intervals_icu.IntervalsClient] | None = None,
    ) -> None:
        self.storage = storage
        self.client_factory = client_factory

    def plan(self) -> Plan:
        return self.storage.load_plan()

    def profile_summary(self, today: dt.date) -> ProfileSummary:
        return profile_summary(self.storage.load_profile(), today)

    def overview(self, today: dt.date) -> PlanOverview:
        return plan_overview(self.plan(), self.storage.load_profile(), today)

    def week(self, index: int) -> WeekView:
        return show_week(self.plan(), self.storage.load_profile(), index)

    def amendments(self) -> list[AmendmentView]:
        return list_amendments(self.plan())

    def _mutate(self, mutation: Mutation) -> MutationResult:
        with self.storage.lock():
            outcome = mutation(self.storage.load_plan(), self.storage.load_profile())
            if outcome.stored is not None:
                self.storage.save_plan(outcome.stored)
            return outcome.result

    def generate(
        self,
        today: dt.date,
        start: dt.date | None = None,
        *,
        expected_revision: int | None,
        dry_run: bool = False,
    ) -> MutationResult:
        with self.storage.lock():
            try:
                current: Plan | None = self.storage.load_plan()
            except PlanNotFound:
                current = None
            outcome = generate_plan(
                self.storage.load_profile(),
                today,
                start,
                current=current,
                expected_revision=expected_revision,
                dry_run=dry_run,
            )
            if outcome.stored is not None:
                self.storage.save_plan(outcome.stored)
            return outcome.result

    def amend(
        self,
        today: dt.date,
        request: AmendRequest,
        *,
        expected_revision: int | None,
        dry_run: bool = False,
        force: bool = False,
    ) -> MutationResult:
        return self._mutate(
            lambda plan, profile: amend(
                plan,
                profile,
                today,
                request,
                expected_revision=expected_revision,
                dry_run=dry_run,
                force=force,
            )
        )

    def declare_unavailable(
        self,
        today: dt.date,
        request: UnavailableRequest,
        *,
        expected_revision: int | None,
        dry_run: bool = False,
        force: bool = False,
    ) -> MutationResult:
        return self._mutate(
            lambda plan, profile: declare_unavailable(
                plan,
                profile,
                today,
                request,
                expected_revision=expected_revision,
                dry_run=dry_run,
                force=force,
            )
        )

    def replan(
        self,
        today: dt.date,
        *,
        expected_revision: int | None,
        dry_run: bool = False,
        force: bool = False,
    ) -> MutationResult:
        return self._mutate(
            lambda plan, profile: replan(
                plan,
                profile,
                today,
                expected_revision=expected_revision,
                dry_run=dry_run,
                force=force,
            )
        )

    def undo(
        self,
        today: dt.date,
        amendment_id: str,
        *,
        expected_revision: int | None,
        dry_run: bool = False,
        force: bool = False,
    ) -> MutationResult:
        return self._mutate(
            lambda plan, profile: undo_amendment(
                plan,
                profile,
                today,
                amendment_id,
                expected_revision=expected_revision,
                dry_run=dry_run,
                force=force,
            )
        )

    def push(self, today: dt.date, request: PushRequest) -> PushReport:
        plan, profile = self.plan(), self.storage.load_profile()
        if request.dry_run:
            return push_plan(plan, profile, today, request)
        if self.client_factory is None:
            raise ValueError("intervals.icu is not configured on this server")
        client = self.client_factory()
        try:
            return push_plan(plan, profile, today, request, client)
        finally:
            client.close()
