"""Property-style test: random valid profiles and goal dates always give a valid plan.

Uses a seeded random.Random (no extra dependency); failures are reproducible by seed.
"""

import datetime as dt
import random
from typing import Any

import pytest

from builders import TODAY, with_exceptions
from endurance_planner.models.enums import GoalType, Priority, Severity, Weekday
from endurance_planner.models.profile import GOAL_SPORT, Profile
from endurance_planner.planner import generate_plan
from endurance_planner.specificity import GOAL_SPECS
from endurance_planner.validation import validate_plan

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MINUTES = [30, 45, 60, 90, 120, 180, 240, 360]


def random_profile(seed: int) -> Profile:
    rng = random.Random(seed)
    goal_type = rng.choice(list(GOAL_SPECS))
    fixed = GOAL_SPORT.get(goal_type)
    goal_sport = fixed.value if fixed else rng.choice(["bike", "run"])
    sports = rng.choice([["bike", "run"]] * 3 + [[goal_sport]])
    training_days = sorted(rng.sample(DAYS, rng.randint(2, 7)), key=DAYS.index)
    weekdays: dict[str, Any] = {}
    for day in training_days:
        minutes = {sport: rng.choice(MINUTES) for sport in sports}
        if len(sports) == 2 and rng.random() < 0.2:
            minutes[rng.choice(sports)] = 0  # single-sport day
        weekdays[day] = minutes

    # The goal sport needs time on at least one day.
    if not any(day.get(goal_sport) for day in weekdays.values()):
        weekdays[training_days[0]][goal_sport] = 60
    weeks_out = rng.randint(GOAL_SPECS[goal_type].min_weeks, 30)
    goal_date = TODAY + dt.timedelta(weeks=weeks_out - 1, days=rng.randint(0, 6))
    goals: list[dict[str, Any]] = [
        {"name": "main", "date": goal_date, "type": goal_type.value, "sport": goal_sport}
    ]
    if rng.random() < 0.4:
        minor = TODAY + dt.timedelta(days=rng.randint(7, (goal_date - TODAY).days - 1))
        priority = rng.choice(["B", "C"])
        goals.append(
            {"name": "minor", "date": minor, "type": goal_type.value, "priority": priority}
        )
    if rng.random() < 0.3:
        later = goal_date + dt.timedelta(weeks=rng.randint(1, 16), days=rng.randint(0, 6))
        goals.append(
            {"name": "second", "date": later, "type": goal_type.value, "sport": goal_sport}
        )
    for goal in goals:
        if GOAL_SPORT.get(GoalType(goal["type"])):
            goal.pop("sport", None)

    thresholds: dict[str, Any] = {
        "target_mode": {
            "bike": rng.choice(["power", "power", "hr", "rpe"]),
            "run": rng.choice(["pace", "pace", "hr", "rpe"]),
        }
    }
    if "bike" in sports:
        thresholds["bike"] = {"ftp_w": rng.randint(120, 400), "lthr_bpm": 165, "max_hr_bpm": 185}
    if "run" in sports:
        pace = rng.randint(200, 420)
        thresholds["run"] = {"threshold_pace": pace, "lthr_bpm": 170, "max_hr_bpm": 190}
    return Profile.model_validate(
        {
            "athlete": {
                "name": f"Athlete {seed}",
                "experience": rng.choice(["beginner", "intermediate", "advanced"]),
                "injury_notes": rng.choice(["", "", "old ankle sprain"]),
            },
            "thresholds": thresholds,
            "current_fitness": {
                "weekly_hours": {
                    "bike": rng.choice([0, 1, 3, 6, 10]) if "bike" in sports else 0,
                    "run": rng.choice([0, 0.5, 2, 4]) if "run" in sports else 0,
                },
                "longest_recent_session_min": {
                    "bike": rng.choice([0, 60, 120, 240]) if "bike" in sports else 0,
                    "run": rng.choice([0, 30, 60, 120]) if "run" in sports else 0,
                },
                "weeks_of_consistent_training": rng.randint(0, 12),
            },
            "availability": {
                "days_per_week": rng.randint(1, len(training_days)),
                "weekly_hours_budget": rng.choice([2, 4, 6, 8, 10, 14]),
                "weekdays": weekdays,
                "long_session_day": rng.choice(training_days),
                "indoor_trainer_days": [
                    day for day in training_days if weekdays[day].get("bike") and rng.random() < 0.3
                ],
                "swap_rules": {
                    "allow_run_instead_of_bike": rng.random() < 0.8,
                    "allow_bike_instead_of_run": rng.random() < 0.8,
                    "max_runs_per_week": rng.randint(0, 5),
                    "no_hard_run_after_hard_bike": rng.random() < 0.8,
                },
            },
            "goals": goals,
            "preferences": {
                "primary_sport": rng.choice(sports),
                "intensity_ratio": rng.choice(
                    [
                        {"low": 0.8, "high": 0.2},
                        {"low": 0.9, "high": 0.1},
                        {"low": 0.75, "high": 0.25},
                    ]
                ),
                "mesocycle_pattern": rng.choice(["3:1", "2:1"]),
                "max_hard_sessions_per_week": rng.randint(1, 3),
                "limits": {
                    "max_weekly_ramp": rng.choice([0.05, 0.08, 0.10]),
                    "max_run_volume_increase": rng.choice([0.05, 0.10]),
                    "recovery_week_factor": rng.choice([0.6, 0.65, 0.7]),
                    "run_impact_multiplier": rng.choice([1.0, 1.25, 1.5]),
                },
            },
        }
    )


@pytest.mark.parametrize("seed", range(150))
def test_random_profiles_give_valid_plans(seed: int) -> None:
    profile = random_profile(seed)
    plan = generate_plan(profile, TODAY)  # raises PlanValidationError on any rule violation
    errors = [i for i in validate_plan(plan, profile) if i.severity is Severity.ERROR]
    assert errors == []
    assert plan.weeks[-1].races
    budget_s = profile.availability.weekly_hours_budget * 3600
    for week in plan.weeks:
        assert week.duration_s <= budget_s
        assert len(week.sessions) <= profile.availability.days_per_week
        for session in week.sessions:
            day = Weekday(DAYS[session.date.weekday()])
            assert session.duration_s <= profile.availability.weekdays[day].of(session.sport) * 60
            assert session.rationale


def random_exceptions(profile: Profile, seed: int) -> list[dict[str, Any]]:
    """1 to 3 non-overlapping calendar exceptions inside the plan, none 'off' on an A race.

    Drawn from their own random stream, so random_profile(seed) stays the same.
    """
    rng = random.Random(10_000 + seed)
    last_goal = max(goal.date for goal in profile.goals)
    span = (last_goal - TODAY).days
    a_races = {goal.date for goal in profile.goals if goal.priority is Priority.A}
    taken: list[tuple[dt.date, dt.date]] = []
    exceptions: list[dict[str, Any]] = []
    for _ in range(rng.randint(1, 3)):
        start = TODAY + dt.timedelta(days=rng.randint(0, span))
        end = start + dt.timedelta(days=rng.choice([0, 0, 1, 3, 6, 9, 13, 16]))
        if any(start <= b and a <= end for a, b in taken):
            continue
        exception: dict[str, Any] = {"from": start.isoformat(), "note": "random"}
        if end != start:
            exception["to"] = end.isoformat()
        off_race = any(start <= race <= end for race in a_races)
        if rng.random() < 0.4 and not off_race:
            exception["mode"] = "off"
        else:
            exception["mode"] = "limited"
            sports = rng.sample(["bike", "run"], rng.randint(1, 2))
            exception["minutes"] = {s: rng.choice([0, 0, 30, 45, 60, 120, 300]) for s in sports}
            if rng.random() < 0.4:
                exception["bike_activity"] = rng.choice(["road", "mtb", "trainer"])
        taken.append((start, end))
        exceptions.append(exception)
    return exceptions


@pytest.mark.parametrize("seed", range(150))
def test_random_profiles_with_exceptions_give_valid_plans(seed: int) -> None:
    profile = with_exceptions(random_profile(seed), random_exceptions(random_profile(seed), seed))
    plan = generate_plan(profile, TODAY)
    errors = [i for i in validate_plan(plan, profile) if i.severity is Severity.ERROR]
    assert errors == []
    availability = profile.availability
    for week in plan.weeks:
        assert len(week.sessions) <= availability.days_per_week
        for session in week.sessions:
            assert session.duration_s <= availability.minutes(session.date, session.sport) * 60
