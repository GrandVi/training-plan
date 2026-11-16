"""Macrocycle skeleton: phases and recovery weeks, built backwards from the goal dates.

The top-priority goals (A, or the best priority present) each drive one
macrocycle: Base -> Build -> Peak -> Taper, with a Transition week between two
macrocycles. Other goals are raced inside the plan with a mini-taper.
"""

import datetime as dt
from dataclasses import dataclass, replace

from endurance_planner.models.enums import Phase, Severity
from endurance_planner.models.issues import Issue
from endurance_planner.models.profile import Goal, Profile
from endurance_planner.specificity import GOAL_SPECS


class PlanningError(ValueError):
    """The plan cannot be generated safely; the message says why and what would work."""


@dataclass(frozen=True)
class WeekSkeleton:
    index: int  # 1-based
    start: dt.date  # Monday
    phase: Phase
    recovery: bool
    goal: Goal  # goal driving this macrocycle
    volume_factor: float = 1.0  # taper weeks: volume relative to the pre-taper level
    race: Goal | None = None  # the driving goal, when raced this week
    minor_races: tuple[Goal, ...] = ()  # lower-priority goals raced this week


def monday(date: dt.date) -> dt.date:
    return date - dt.timedelta(days=date.weekday())


def default_start(today: dt.date) -> dt.date:
    """First Monday on or after today."""
    return today + dt.timedelta(days=(7 - today.weekday()) % 7)


def split_phases(weeks: int, split: tuple[float, float, float]) -> tuple[int, int, int]:
    """(base, build, peak) week counts for the pre-taper part of a macrocycle."""
    build = round(weeks * split[1])
    peak = round(weeks * split[2])
    if weeks >= 3:
        build, peak = max(build, 1), max(peak, 1)
    base = weeks - build - peak
    if base < 0:
        build, base = build + base, 0
    return base, build, peak


def recovery_flags(weeks: int, loading_weeks: int) -> list[bool]:
    """Recovery weeks every (loading_weeks + 1) weeks, anchored so that the last block
    before the taper is a full loading block. The first week is never a recovery week."""
    cycle = loading_weeks + 1
    return [index > 0 and (weeks - 1 - index) % cycle == loading_weeks for index in range(weeks)]


def build_skeleton(profile: Profile, start: dt.date) -> tuple[list[WeekSkeleton], list[Issue]]:
    """Week-by-week skeleton from `start` (a Monday) to the last driving goal."""
    issues: list[Issue] = []
    upcoming = [goal for goal in profile.goals if goal.date >= start]
    if not upcoming:
        raise PlanningError(f"no goal on or after the plan start {start}")
    for goal in upcoming:
        if goal.type not in GOAL_SPECS:
            raise PlanningError(f"goal type '{goal.type}' is not supported yet")
    top = min(goal.priority for goal in upcoming)
    drivers = [goal for goal in upcoming if goal.priority == top]
    loading_weeks = int(profile.preferences.mesocycle_pattern[0])

    weeks: list[WeekSkeleton] = []
    segment_start = start
    for position, goal in enumerate(drivers):
        spec = GOAL_SPECS[goal.type]
        count = (monday(goal.date) - segment_start).days // 7 + 1
        if count <= 0:
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    code="goal_same_week",
                    message=f"'{goal.name}' is in the same week as the previous goal; "
                    "it is raced without its own build-up",
                )
            )
            continue
        if count < spec.min_weeks:
            if position == 0:
                earliest = segment_start + dt.timedelta(
                    weeks=spec.min_weeks - 1, days=goal.date.weekday()
                )
                raise PlanningError(
                    f"'{goal.name}' ({goal.date}) is {count} week(s) away from the plan start "
                    f"{start}; a safe {goal.type} progression needs at least {spec.min_weeks}. "
                    f"Closest feasible option: a race on or after {earliest}, or give this goal "
                    "priority B to race it inside a longer plan."
                )
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    code="compressed_macrocycle",
                    message=f"'{goal.name}' is only {count} week(s) after the previous goal "
                    f"(recommended: {spec.min_weeks}); phases are compressed",
                )
            )
        taper = min(len(spec.taper_factors), count)
        transition = 1 if position > 0 and count > taper else 0
        build_up = count - taper - transition
        base, build, peak = split_phases(build_up, spec.phase_split)
        phases = (
            [Phase.TRANSITION] * transition
            + [Phase.BASE] * base
            + [Phase.BUILD] * build
            + [Phase.PEAK] * peak
            + [Phase.TAPER] * taper
        )
        recovery = [False] * transition + recovery_flags(build_up, loading_weeks) + [False] * taper
        factors = [1.0] * (count - taper) + list(spec.taper_factors[-taper:])
        for offset in range(count):
            weeks.append(
                WeekSkeleton(
                    index=len(weeks) + 1,
                    start=segment_start + dt.timedelta(weeks=offset),
                    phase=phases[offset],
                    recovery=recovery[offset],
                    goal=goal,
                    volume_factor=factors[offset],
                    race=goal if offset == count - 1 else None,
                )
            )
        segment_start += dt.timedelta(weeks=count)

    if not weeks:
        raise PlanningError("no plannable goal")
    end = weeks[-1].start + dt.timedelta(days=6)
    minors = [goal for goal in upcoming if goal not in drivers and goal.date <= end]
    skipped = [goal for goal in drivers if all(week.race != goal for week in weeks)]
    for goal in upcoming:
        if goal not in drivers and goal.date > end:
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    code="goal_not_covered",
                    message=f"'{goal.name}' ({goal.date}) is after the last main goal and is "
                    "not covered by this plan",
                )
            )
    for goal in [*minors, *skipped]:
        index = (monday(goal.date) - start).days // 7
        week = weeks[index]
        weeks[index] = replace(week, minor_races=(*week.minor_races, goal))
    return weeks, issues
