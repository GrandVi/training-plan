"""Shared test constants and object builders."""

import datetime as dt
from pathlib import Path

from endurance_planner.models.enums import ActivityType, Intent, Slot, Sport, StepKind, TargetKind
from endurance_planner.models.plan import Repeat, Session, Step, Target

EXAMPLE = Path(__file__).parent.parent / "examples" / "profile.example.yaml"
TODAY = dt.date(2026, 11, 16)


def step(
    duration_s: int,
    low: float,
    high: float,
    kind: TargetKind = TargetKind.POWER,
    step_kind: StepKind = StepKind.WORK,
    distance_m: int | None = None,
) -> Step:
    return Step(
        kind=step_kind,
        duration_s=duration_s,
        distance_m=distance_m,
        target=Target(kind=kind, low=low, high=high),
    )


def session(steps: list[Step | Repeat], sport: Sport = Sport.BIKE) -> Session:
    return Session(
        date=dt.date(2026, 12, 1),
        sport=sport,
        activity_type=ActivityType.RIDE if sport is Sport.BIKE else ActivityType.RUN,
        slot=Slot.KEY_1,
        intent=Intent.THRESHOLD,
        name="test session",
        steps=steps,
        rationale="test",
    )


def two_by_twenty() -> Session:
    """15 min warm-up, 2 x (20 min @ 95-100% FTP + 5 min easy), 10 min cool-down."""
    return session(
        [
            step(900, 0.55, 0.65, step_kind=StepKind.WARMUP),
            Repeat(
                count=2,
                steps=[
                    step(1200, 0.95, 1.00),
                    step(300, 0.50, 0.60, step_kind=StepKind.RECOVERY),
                ],
            ),
            step(600, 0.50, 0.60, step_kind=StepKind.COOLDOWN),
        ]
    )
