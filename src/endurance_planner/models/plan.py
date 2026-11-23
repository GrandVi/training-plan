"""Plan structure: Plan > Week > Session > Step | Repeat.

Targets are always stored relative to the athlete's thresholds. Absolute
values live in Step.resolved and can be recomputed at any time (see targets.py).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    computed_field,
    model_validator,
)

from endurance_planner.load import steps_tss
from endurance_planner.models.enums import (
    ActivityType,
    AmendmentKind,
    BikeActivity,
    Equipment,
    Intent,
    Pattern,
    Phase,
    Scope,
    Slot,
    Sport,
    StepKind,
    TargetKind,
)
from endurance_planner.models.issues import Issue
from endurance_planner.models.profile import CalendarException, Goal, Profile
from endurance_planner.units import format_clock


# Fields added after a schema version are not written while they hold their default, so
# files written without them stay byte-identical (Field exclude_if, which also keeps the
# serialization JSON Schema complete).
def _empty(value: list[Any]) -> bool:
    return not value


def _unset(value: object) -> bool:
    return value is None


def _zero(value: int) -> bool:
    return value == 0


def _span(low: str, high: str) -> str:
    return low if low == high else f"{low}-{high}"


class Target(BaseModel):
    """Relative intensity range.

    power: fraction of FTP; pace: fraction of threshold speed; hr: fraction of
    LTHR; rpe: 1-10.
    """

    kind: TargetKind
    low: float
    high: float

    @model_validator(mode="after")
    def _check(self) -> Self:
        lo, hi = (1.0, 10.0) if self.kind is TargetKind.RPE else (0.3, 3.0)
        if not lo <= self.low <= self.high <= hi:
            raise ValueError(f"{self.kind} target must satisfy {lo} <= low <= high <= {hi}")
        return self

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def label(self) -> str:
        if self.kind is TargetKind.RPE:
            return f"RPE {_span(f'{self.low:g}', f'{self.high:g}')}"
        unit = {TargetKind.POWER: "FTP", TargetKind.PACE: "threshold pace", TargetKind.HR: "LTHR"}
        return f"{_span(f'{self.low * 100:.0f}', f'{self.high * 100:.0f}')}% {unit[self.kind]}"


class ResolvedTarget(BaseModel):
    """Absolute values of a Target for the current thresholds."""

    power_w: tuple[int, int] | None = None  # (low, high)
    pace_s_per_km: tuple[int, int] | None = None  # (slow, fast)
    hr_bpm: tuple[int, int] | None = None  # (low, high); a guide when the target is not HR

    def label(self) -> str:
        parts: list[str] = []
        if self.power_w:
            parts.append(_span(*map(str, self.power_w)) + " W")
        if self.pace_s_per_km:
            slow, fast = self.pace_s_per_km
            parts.append(_span(format_clock(fast), format_clock(slow)) + " /km")
        if self.hr_bpm:
            parts.append(_span(*map(str, self.hr_bpm)) + " bpm")
        return ", ".join(parts)


class Step(BaseModel):
    kind: StepKind = StepKind.WORK
    duration_s: int = Field(gt=0)
    # If set, the step is prescribed by distance and duration_s is an estimate
    # (recomputed from the threshold pace when targets are resolved).
    distance_m: int | None = Field(None, gt=0)
    target: Target
    note: str = ""
    resolved: ResolvedTarget | None = None


class Repeat(BaseModel):
    count: int = Field(ge=1)
    steps: list[Step | Repeat] = Field(min_length=1)


def flatten(blocks: Iterable[Step | Repeat]) -> Iterator[Step]:
    """Expand (nested) repeats into a flat sequence of steps."""
    for block in blocks:
        if isinstance(block, Repeat):
            for _ in range(block.count):
                yield from flatten(block.steps)
        else:
            yield block


class Session(BaseModel):
    date: dt.date
    sport: Sport
    activity_type: ActivityType
    slot: Slot
    intent: Intent
    name: str
    steps: list[Step | Repeat] = Field(min_length=1)
    rationale: str = Field(min_length=1)  # why this session, why now
    template_id: str | None = None
    indoor: bool = False
    technical: bool = False  # skills session: cannot be swapped to the other sport
    notes: str = ""
    # Schema v2. Ladder level this key session completes (None: not a key session, or an
    # amended key session that does not count, so its ladder does not advance).
    rung: int | None = Field(None, ge=0)
    pinned: bool = False  # set by an amendment: a replan keeps the session as it is
    amendment_id: str | None = None  # amendment that produced this session
    # F8. "actual": what was done (plan adapt), not a planned session; not written if unset.
    source: str | None = Field(None, exclude_if=_unset)

    @property
    def actual(self) -> bool:
        return self.source == "actual"

    def flat_steps(self) -> Iterator[Step]:
        return flatten(self.steps)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_s(self) -> int:
        return sum(step.duration_s for step in self.flat_steps())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tss(self) -> float:
        return round(steps_tss(self.flat_steps(), self.sport), 1)


class Exercise(BaseModel):
    """One exercise of a strength session: sets x reps (or seconds) at a reps-in-reserve."""

    id: str  # strength.EXERCISES key
    name: str
    pattern: Pattern
    sets: int = Field(ge=1)
    reps: int = Field(ge=1)
    unit: Literal["reps", "s"] = "reps"  # "s": a hold, `reps` is seconds
    rir: int | None = Field(None, ge=0, le=5)  # reps in reserve; None for holds
    per_side: bool = False
    accessory: bool = False  # dropped first when the session is shortened
    note: str = ""

    def label(self) -> str:
        amount = f"{self.reps} s" if self.unit == "s" else f"{self.reps} reps"
        side = " each side" if self.per_side else ""
        effort = f", {self.rir} reps in reserve" if self.rir is not None else ""
        return f"{self.name}: {self.sets} x {amount}{side}{effort}"


class StrengthSession(BaseModel):
    """A strength session (not a Sport: it stays out of the endurance load and ramp caps)."""

    date: dt.date
    name: str
    phase: Phase
    template_id: str  # strength.TEMPLATES key
    variant: str = "full"  # A, B or full: which movement patterns it covers
    exercises: list[Exercise] = Field(min_length=1)
    minutes: int = Field(ge=1)
    rationale: str = Field(min_length=1)
    equipment_used: list[Equipment] = Field(default_factory=list)
    rung: int | None = Field(None, ge=0)  # ladder level it completes (None: does not count)
    pinned: bool = False
    amendment_id: str | None = None
    source: str | None = Field(None, exclude_if=_unset)  # "actual": done (plan adapt)

    @property
    def actual(self) -> bool:
        return self.source == "actual"


class Week(BaseModel):
    index: int = Field(ge=1)
    start: dt.date  # Monday
    phase: Phase
    recovery: bool = False
    goal: str = ""  # name of the goal driving this macrocycle
    races: list[str] = Field(default_factory=list)  # goals raced this week
    sessions: list[Session] = Field(default_factory=list)
    # Strength sessions (F10). Not written while empty: plans without strength are unchanged.
    strength: list[StrengthSession] = Field(default_factory=list, exclude_if=_empty)
    # F8. Planned sessions of the days replaced by what was done (plan adapt): the rules
    # compare the actual week with them (overreach guard, under-training). Not written empty.
    planned_sessions: list[Session] = Field(default_factory=list, exclude_if=_empty)
    planned_strength: list[StrengthSession] = Field(default_factory=list, exclude_if=_empty)

    @property
    def has_actual(self) -> bool:
        """Part of the week is what was done, not a plan (see plan adapt)."""
        return (
            bool(self.planned_sessions)
            or bool(self.planned_strength)
            or any(s.actual for s in self.sessions)
        )

    @property
    def strength_minutes(self) -> int:
        return sum(session.minutes for session in self.strength)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_s(self) -> int:
        return sum(session.duration_s for session in self.sessions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tss(self) -> float:
        return round(sum(session.tss for session in self.sessions), 1)


class Amendment(BaseModel):
    """A change requested on a running plan, kept in Plan.amendments (see amend.py).

    Session amendments edit, skip or move the session planned on `date`; their result
    is pinned in the plan. Strength amendments do the same for the strength session of
    `date` (minutes, skip, move, equipment). Unavailable amendments add a calendar exception
    from `date` to `to` (inclusive). A later amendment of the same session supersedes the
    earlier one; both stay in the log.
    """

    id: str
    kind: AmendmentKind = AmendmentKind.SESSION
    created: dt.date  # day the amendment was made
    date: dt.date  # planned date of the session; first day of an unavailable range
    to: dt.date | None = None  # unavailable: last day (inclusive)
    skip: bool = False
    move_to: dt.date | None = None
    minutes: int | None = Field(None, gt=0)  # new duration of the session
    sport: Sport | None = None
    activity: BikeActivity | None = None
    intent: Intent | None = None  # easier intent (endurance or recovery)
    day_minutes: dict[Sport, int] = Field(default_factory=dict)  # unavailable; {} = off
    scope: Scope = Scope.REST
    reason: str = ""
    force: bool = False  # applied although it breaks a rule
    original: Session | None = None  # session as planned before any amendment
    replaces: str | None = None  # amendment of the same session it superseded
    superseded_by: str | None = None
    # Strength amendments (F10); not written while unset, so older logs are unchanged.
    equipment: list[Equipment] | None = Field(None, exclude_if=_unset)  # available for it
    original_strength: StrengthSession | None = Field(None, exclude_if=_unset)  # as planned

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    @property
    def last(self) -> dt.date:
        """Last day of an unavailable range."""
        return self.to or self.date

    def as_exception(self) -> CalendarException:
        """Calendar exception of an unavailable amendment (same rules as the profile's)."""
        off = not self.day_minutes and self.activity is None
        return CalendarException(
            start=self.date,
            end=self.to,
            mode="off" if off else "limited",
            minutes=dict(self.day_minutes),
            bike_activity=self.activity,
            note=self.reason or f"amendment {self.id}",
        )


PLAN_SCHEMA_VERSION = 2


def migrate_plan_data(data: Any) -> Any:
    """Upgrade a stored plan (parsed JSON) to the current schema, in memory.

    v1 -> v2: Session.rung / pinned / amendment_id and Plan.amendments are new optional
    fields; their defaults describe a v1 plan exactly.
    """
    if not isinstance(data, dict):
        return data
    version = data.get("schema_version", 1)
    if not isinstance(version, int) or not 1 <= version <= PLAN_SCHEMA_VERSION:
        raise ValueError(
            f"plan schema_version {version!r} is not supported (this version reads 1 to "
            f"{PLAN_SCHEMA_VERSION}); update endurance-planner"
        )
    if version < PLAN_SCHEMA_VERSION:
        data = {**data, "schema_version": PLAN_SCHEMA_VERSION}
    return data


class Plan(BaseModel):
    id: str
    schema_version: int = PLAN_SCHEMA_VERSION
    created: dt.date
    athlete: str
    goals: list[Goal]
    weeks: list[Week]
    warnings: list[Issue] = Field(default_factory=list)
    amendments: list[Amendment] = Field(default_factory=list)
    # Version of the stored plan: every saved change made through the services adds 1, and a
    # change based on an older revision is refused (services.RevisionConflict). Files without
    # it read as 0, and 0 is not written, so a freshly generated plan file is unchanged.
    revision: int = Field(0, ge=0, exclude_if=_zero)
    # F8. Days off found by plan adapt (7+ days without any activity, calendar events); like
    # the plan's unavailable ranges, they count as calendar exceptions. Not written if empty.
    days_off: list[CalendarException] = Field(default_factory=list, exclude_if=_empty)

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, data: Any) -> Any:
        return migrate_plan_data(data)

    def active_amendments(self, kind: AmendmentKind | None = None) -> list[Amendment]:
        return [a for a in self.amendments if a.active and (kind is None or a.kind is kind)]

    def effective_profile(self, profile: Profile) -> Profile:
        """The profile with this plan's days off (plan adapt) and unavailable amendments added
        to its calendar exceptions (they come last, so they win where they overlap a profile
        exception)."""
        extra = [
            *self.days_off,
            *(a.as_exception() for a in self.active_amendments(AmendmentKind.UNAVAILABLE)),
        ]
        if not extra:
            return profile
        exceptions = [*profile.availability.exceptions, *extra]
        availability = profile.availability.model_copy(update={"exceptions": exceptions})
        return profile.model_copy(update={"availability": availability})
