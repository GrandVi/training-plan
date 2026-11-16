"""Plan structure: Plan > Week > Session > Step | Repeat.

Targets are always stored relative to the athlete's thresholds. Absolute
values live in Step.resolved and can be recomputed at any time (see targets.py).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from typing import Self

from pydantic import BaseModel, Field, computed_field, model_validator

from endurance_planner.load import steps_tss
from endurance_planner.models.enums import (
    ActivityType,
    Intent,
    Phase,
    Slot,
    Sport,
    StepKind,
    TargetKind,
)
from endurance_planner.models.issues import Issue
from endurance_planner.models.profile import Goal
from endurance_planner.units import format_clock


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


def _span(low: str, high: str) -> str:
    return low if low == high else f"{low}-{high}"


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


class Week(BaseModel):
    index: int = Field(ge=1)
    start: dt.date  # Monday
    phase: Phase
    recovery: bool = False
    sessions: list[Session] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_s(self) -> int:
        return sum(session.duration_s for session in self.sessions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tss(self) -> float:
        return round(sum(session.tss for session in self.sessions), 1)


class Plan(BaseModel):
    id: str
    schema_version: int = 1
    created: dt.date
    athlete: str
    goals: list[Goal]
    weeks: list[Week]
    warnings: list[Issue] = Field(default_factory=list)
