"""Plan generation entry point (pure library code, used by the CLI and by future front-ends)."""

import datetime as dt
import re

from endurance_planner.models.enums import Severity, Sport
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Plan, Week
from endurance_planner.models.profile import Profile
from endurance_planner.periodization import PlanningError, build_skeleton, default_start
from endurance_planner.scheduler import Scheduler
from endurance_planner.specificity import GOAL_SPECS
from endurance_planner.targets import resolve_plan
from endurance_planner.thresholds import resolve_thresholds
from endurance_planner.validation import assert_valid


def _feasibility_warnings(profile: Profile, goal_names: set[str]) -> list[Issue]:
    """Say explicitly when availability is below what a goal typically needs."""
    issues: list[Issue] = []
    availability = profile.availability
    for goal in profile.goals:
        if goal.name not in goal_names:
            continue
        spec = GOAL_SPECS[goal.type]
        sport = profile.goal_sport(goal)
        if availability.weekly_hours_budget < spec.recommended_hours:
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    code="budget_below_goal_needs",
                    message=f"'{goal.name}' ({goal.type}) typically needs about "
                    f"{spec.recommended_hours:g} h/week at peak; the budget is "
                    f"{availability.weekly_hours_budget:g} h. This is the best feasible plan.",
                )
            )
        long_minutes = availability.weekdays[availability.long_session_day].of(sport)
        wanted = spec.long_max_min[sport]
        if goal.target.duration_h:
            wanted = min(wanted, round(0.8 * goal.target.duration_h * 60))
        if long_minutes < wanted:
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    code="long_day_below_goal_needs",
                    message=f"'{goal.name}': long {sport} sessions should reach about {wanted} min "
                    f"but {availability.long_session_day} allows {long_minutes} min.",
                )
            )
    return issues


def plan_id(profile: Profile, start: dt.date) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", profile.athlete.name.lower()).strip("-") or "athlete"
    return f"{slug}-{start:%Y%m%d}"


def generate_plan(profile: Profile, today: dt.date, start: dt.date | None = None) -> Plan:
    """Generate, resolve and validate a plan. Deterministic for a given (profile, today, start).

    Raises PlanningError when no safe plan exists and PlanValidationError if the
    result breaks a hard rule (which would be a bug in the scheduler).
    """
    start = start or default_start(today)
    if start.weekday() != 0 or start < today:
        raise PlanningError(f"plan start {start} must be a Monday, not before today ({today})")
    thresholds, issues = resolve_thresholds(profile, today)
    skeleton, skeleton_issues = build_skeleton(profile, start)
    scheduler = Scheduler(profile, thresholds)
    weeks: list[Week] = []
    for week in skeleton:
        weeks.append(scheduler.build_week(week, weeks))

    covered = {name for week in weeks for name in (week.goal, *week.races)}
    issues += skeleton_issues + _feasibility_warnings(profile, covered) + scheduler.issues
    if scheduler.capped_keys:
        days = ", ".join(day.value for day in scheduler.capped_keys)
        issues.append(
            Issue(
                severity=Severity.WARNING,
                code="key_sessions_capped_by_day",
                message=f"{sum(scheduler.capped_keys.values())} key session(s) were shortened "
                f"to fit the time available on {days}; more minutes on those days would let "
                "the intervals progress further",
            )
        )
    for sport in Sport:
        if sport not in scheduler.enabled:
            has_time = any(day.of(sport) for day in profile.availability.weekdays.values())
            if has_time:
                issues.append(
                    Issue(
                        severity=Severity.WARNING,
                        code="sport_without_threshold",
                        message=f"{sport} has available time but no usable threshold; "
                        f"no {sport} session is planned",
                    )
                )
    plan = Plan(
        id=plan_id(profile, start),
        created=today,
        athlete=profile.athlete.name,
        goals=profile.goals,
        weeks=weeks,
        warnings=issues,
    )
    plan.warnings.extend(assert_valid(plan, profile, today))
    return plan


def retarget_plan(plan: Plan, profile: Profile, today: dt.date) -> tuple[Plan, list[Issue]]:
    """Recompute all absolute targets from the profile's current thresholds."""
    thresholds, issues = resolve_thresholds(profile, today)
    return resolve_plan(plan, thresholds), issues
