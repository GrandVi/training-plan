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


def ultra_profile(sport: str = "bike") -> Profile:
    """Scenario: ultra-endurance event in 20 weeks."""
    return make_profile(
        goals=[
            goal(
                "Ultra",
                dt.date(2027, 4, 3),
                "ultra_endurance",
                sport=sport,
                target={"duration_h": 12},
            )
        ],
        edits={
            "preferences.primary_sport": sport,
            "availability.weekly_hours_budget": 12,
            "availability.weekdays.sat": {"bike": 420, "run": 240},
            "availability.weekdays.sun": {"bike": 300, "run": 150},
            "availability.swap_rules.max_runs_per_week": 4,
            "current_fitness.weekly_hours": {"bike": 7, "run": 3},
            "current_fitness.longest_recent_session_min": {"bike": 210, "run": 100},
        },
    )


def golden_session(template_id: str, level: int, mode: str | None = None) -> Session:
    """A key session of the example athlete, resolved, for text-rendering tests."""
    from endurance_planner.targets import express_blocks, resolve_session
    from endurance_planner.templates import TEMPLATES, key_blocks
    from endurance_planner.thresholds import resolve_thresholds

    template = TEMPLATES[template_id]
    blocks = key_blocks(template, level, pace_s=255, goal_pace=1.037)
    if mode is not None:
        blocks = express_blocks(blocks, template.sport, mode)
    base = session(blocks, template.sport).model_copy(
        update={"rationale": template.rationale, "notes": template.notes, "name": template.name}
    )
    return resolve_session(base, resolve_thresholds(make_profile(), TODAY)[0])


def with_exceptions(profile: Profile, exceptions: list[dict[str, Any]]) -> Profile:
    """The profile with availability.exceptions replaced (written as in profile.yaml)."""
    data = profile.model_dump(mode="json", by_alias=True)
    data["availability"]["exceptions"] = exceptions
    return Profile.model_validate(data)


def free_long_day_profile() -> Profile:
    """MTB marathon without a duration target: the long ride is capped by Saturday's 240 min."""
    return make_profile(
        goals=[goal("MTB marathon", dt.date(2027, 3, 7), "mtb_marathon")],
        edits={
            "current_fitness.weekly_hours": {"bike": 8, "run": 0.75},
            "current_fitness.longest_recent_session_min": {"bike": 220, "run": 40},
            "availability.weekly_hours_budget": 11,
        },
    )


def with_strength(profile: Profile, **settings: Any) -> Profile:
    """The profile with availability.strength set (2 sessions a week by default)."""
    data = profile.model_dump(mode="json", by_alias=True)
    data["availability"]["strength"] = {"sessions_per_week": 2, **settings}
    return Profile.model_validate(data)
