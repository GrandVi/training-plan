"""Shared test constants and object builders."""

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

from endurance_planner.models.enums import ActivityType, Intent, Slot, Sport, StepKind, TargetKind
from endurance_planner.models.plan import Repeat, Session, Step, Target
from endurance_planner.models.profile import Profile

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


def example_data() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def edit(data: dict[str, Any], path: str, value: object) -> None:
    """Set a value by dotted path, e.g. edit(data, "availability.days_per_week", 4)."""
    *parents, leaf = path.split(".")
    node = data
    for key in parents:
        node = node[key]
    node[leaf] = value


def goal(
    name: str, date: dt.date, goal_type: str, priority: str = "A", **extra: Any
) -> dict[str, Any]:
    return {"name": name, "date": date, "type": goal_type, "priority": priority, **extra}


def make_profile(
    goals: list[dict[str, Any]] | None = None, edits: dict[str, object] | None = None
) -> Profile:
    """The example profile with other goals and dotted-path edits."""
    data = example_data()
    if goals is not None:
        data["goals"] = goals
    for path, value in (edits or {}).items():
        edit(data, path, value)
    return Profile.model_validate(data)


def run_10k_profile() -> Profile:
    """Scenario: 10k in 12 weeks, run primary, 4 days a week, bike as support."""
    return make_profile(
        goals=[goal("10k", dt.date(2027, 2, 7), "run_10k", target={"time": "41:00"})],
        edits={
            "preferences.primary_sport": "run",
            "availability.days_per_week": 4,
            "availability.weekly_hours_budget": 6,
            "availability.swap_rules.max_runs_per_week": 4,
            "current_fitness.weekly_hours": {"bike": 2, "run": 3},
            "current_fitness.longest_recent_session_min": {"bike": 90, "run": 70},
        },
    )


def mtb_marathon_profile() -> Profile:
    """Scenario: MTB marathon in 16 weeks, bike primary, 5 days a week including one run."""
    return make_profile(
        goals=[
            goal(
                "MTB marathon",
                dt.date(2027, 3, 7),
                "mtb_marathon",
                target={"duration_h": 4.5, "elevation_m": 2500},
            )
        ],
        edits={"current_fitness.weekly_hours": {"bike": 7, "run": 0.75}},
    )
