"""Sport swap: convert a session to the other sport.

Conversion rules (explicit, per step):
1. Intensity is mapped through the HR-equivalence tables of zones.py
   (bike %FTP -> %LTHR -> run % threshold speed, and back), so a step stays in
   the same physiological domain. HR and RPE targets are kept as they are.
2. Duration is scaled so that the impact-weighted load of the step is preserved:
   run TSS x run_impact_multiplier = bike TSS. Bike -> run therefore shortens
   the step, run -> bike lengthens it (3 x 12 min at FTP <-> about 3 x 10 min
   at run threshold with the default multiplier of 1.25).
3. Bike -> run only: a repeat made exclusively of efforts above LT1 (e.g.
   over-unders) becomes one continuous cruise block at the mean intensity.
4. Warm-up and cool-down use the destination sport's standard intensity, with a
   duration kept between its short and default lengths.
5. A technical session cannot keep its intent: it becomes plain endurance and a
   warning is returned.
"""

import datetime as dt
import re

from endurance_planner.load import intensity_factor, is_low_intensity, run_impact_multiplier
from endurance_planner.models.enums import (
    ActivityType,
    Intent,
    Severity,
    Sport,
    StepKind,
    TargetKind,
    Weekday,
)
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Plan, Repeat, Session, Step, Target, flatten
from endurance_planner.models.profile import Profile
from endurance_planner.targets import NATIVE_KIND, express_blocks, resolve_session
from endurance_planner.templates import COOLDOWN_S, RECOVERY, WARMUP, WARMUP_S
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.validation import validate_plan
from endurance_planner.zones import DEFAULT_ZONES, ZoneSet, hr_to_if, if_to_hr, zone_set

_MARK = re.compile(r" \[from (bike|run)\]$")


class SwapError(ValueError):
    """The requested swap is not possible (no session, sport not allowed or not available)."""


def other_sport(sport: Sport) -> Sport:
    return Sport.RUN if sport is Sport.BIKE else Sport.BIKE


def map_intensity(value: float, source: Sport, dest: Sport) -> float:
    """Equivalent intensity factor in the other sport (same fraction of LTHR)."""
    return hr_to_if(if_to_hr(value, source), dest)


def convert_target(target: Target, source: Sport, dest: Sport) -> Target:
    if target.kind in (TargetKind.HR, TargetKind.RPE):
        return target
    return Target(
        kind=NATIVE_KIND[dest],
        low=round(map_intensity(target.low, source, dest), 2),
        high=round(map_intensity(target.high, source, dest), 2),
    )


def _round_duration(seconds: float) -> int:
    step = 60 if seconds >= 600 else 30 if seconds >= 120 else 5 if seconds >= 30 else 1
    return max(5, round(seconds / step) * step)


def _impact(sport: Sport, run_impact: float) -> float:
    return run_impact if sport is Sport.RUN else 1.0


def _convert_step(step: Step, source: Sport, dest: Sport, run_impact: float) -> Step:
    target = convert_target(step.target, source, dest)
    if step.kind in (StepKind.WARMUP, StepKind.COOLDOWN):
        low, high = (WARMUP if step.kind is StepKind.WARMUP else RECOVERY)[dest]
        target = Target(kind=NATIVE_KIND[dest], low=low, high=high)
    weight_source = intensity_factor(step.target, source) ** 2 * _impact(source, run_impact)
    weight_dest = intensity_factor(target, dest) ** 2 * _impact(dest, run_impact)
    duration = _round_duration(step.duration_s * weight_source / weight_dest)
    if step.kind in (StepKind.WARMUP, StepKind.COOLDOWN):
        default, short = (WARMUP_S if step.kind is StepKind.WARMUP else COOLDOWN_S)[dest]
        duration = min(max(duration, short), default)
    return Step(kind=step.kind, duration_s=duration, target=target, note=step.note)


def _merge(steps: list[Step]) -> Step:
    total = sum(step.duration_s for step in steps)
    low = sum(step.target.low * step.duration_s for step in steps) / total
    high = sum(step.target.high * step.duration_s for step in steps) / total
    target = Target(kind=steps[0].target.kind, low=round(low, 2), high=round(high, 2))
    return Step(duration_s=_round_duration(total), target=target, note="continuous")


def _convert_blocks(
    blocks: list[Step | Repeat], source: Sport, dest: Sport, run_impact: float, zones: ZoneSet
) -> list[Step | Repeat]:
    out: list[Step | Repeat] = []
    for block in blocks:
        if not isinstance(block, Repeat):
            out.append(_convert_step(block, source, dest, run_impact))
            continue
        flat = list(flatten([block]))
        kinds = {step.target.kind for step in flat}
        all_high = not any(is_low_intensity(step.target, source, zones) for step in flat)
        if dest is Sport.RUN and all_high and len(kinds) == 1:
            out.append(_merge([_convert_step(step, source, dest, run_impact) for step in flat]))
        else:
            steps = _convert_blocks(block.steps, source, dest, run_impact, zones)
            out.append(Repeat(count=block.count, steps=steps))
    return out


def convert_session(
    session: Session,
    run_impact: float,
    target_mode: str | None = None,
    zones: ZoneSet = DEFAULT_ZONES,
) -> tuple[Session, list[str]]:
    """Session converted to the other sport, plus warnings when intent is not preserved.

    target_mode: the athlete's target mode for the destination sport (None = native).
    Absolute targets are cleared; resolve the result with targets.resolve_session.
    """
    source, dest = session.sport, other_sport(session.sport)
    warnings: list[str] = []
    intent = session.intent
    if session.technical:
        warnings.append(
            f"{session.date} {session.name}: technical session, skills work cannot be done "
            f"as a {dest}; converted to plain endurance of equivalent load"
        )
        intent = Intent.ENDURANCE
    steps = _convert_blocks(session.steps, source, dest, run_impact, zones)
    if target_mode is not None:
        steps = express_blocks(steps, dest, target_mode)
    mark = _MARK.search(session.name)
    base = _MARK.sub("", session.name)
    name = base if mark and mark.group(1) == dest.value else f"{base} [from {source.value}]"
    converted = session.model_copy(
        update={
            "sport": dest,
            "activity_type": ActivityType.RUN if dest is Sport.RUN else ActivityType.RIDE,
            "intent": intent,
            "name": name,
            "steps": steps,
            "indoor": False,
            "technical": False,
        }
    )
    return converted, warnings


def _scale_blocks(blocks: list[Step | Repeat], factor: float) -> list[Step | Repeat]:
    out: list[Step | Repeat] = []
    for block in blocks:
        if isinstance(block, Repeat):
            out.append(Repeat(count=block.count, steps=_scale_blocks(block.steps, factor)))
        else:
            duration = _round_duration(block.duration_s * factor)
            out.append(block.model_copy(update={"duration_s": duration, "distance_m": None}))
    return out


def swap_session(
    plan: Plan, date: dt.date, profile: Profile, thresholds: ResolvedThresholds
) -> tuple[Plan, list[Issue]]:
    """Swap the session of `date` to the other sport and re-validate its week.

    Returns the new plan and the issues to show: conversion warnings, plus every
    validation issue that the swap introduced. The caller decides whether errors
    are acceptable (the CLI requires --force).
    """
    located = [
        (week, session) for week in plan.weeks for session in week.sessions if session.date == date
    ]
    if not located:
        raise SwapError(f"no session on {date}")
    week, session = located[0]
    dest = other_sport(session.sport)
    rules = profile.availability.swap_rules
    allowed = (
        rules.allow_run_instead_of_bike if dest is Sport.RUN else rules.allow_bike_instead_of_run
    )
    if not allowed:
        raise SwapError(f"swap_rules do not allow a {dest} instead of a {session.sport}")
    if not profile.thresholds.anchored(dest):
        raise SwapError(f"no usable {dest} threshold in the profile")
    day = list(Weekday)[date.weekday()]
    limit_s = profile.availability.minutes(date, dest) * 60
    if limit_s == 0:
        raise SwapError(f"no {dest} time on {day}")

    mode = profile.thresholds.target_mode
    converted, warnings = convert_session(
        session,
        run_impact_multiplier(profile),
        mode.bike if dest is Sport.BIKE else mode.run,
        zone_set(profile.thresholds.zone_models),
    )
    issues = [
        Issue(severity=Severity.WARNING, code="swap_intent_lost", message=text, week=week.index)
        for text in warnings
    ]
    if converted.duration_s > limit_s:
        before = converted.duration_s
        steps = _scale_blocks(converted.steps, limit_s / before * 0.98)
        converted = converted.model_copy(update={"steps": steps})
        issues.append(
            Issue(
                severity=Severity.WARNING,
                code="swap_shortened",
                message=f"{date} {converted.name}: shortened from {before // 60} to "
                f"{converted.duration_s // 60} min to fit {day}; the load is not fully preserved",
                week=week.index,
            )
        )
    converted = resolve_session(converted, thresholds)

    weeks = [
        w.model_copy(update={"sessions": [converted if s is session else s for s in w.sessions]})
        if w is week
        else w
        for w in plan.weeks
    ]
    swapped = plan.model_copy(update={"weeks": weeks})
    known = {(issue.code, issue.week, issue.message) for issue in validate_plan(plan, profile)}
    issues += [
        issue
        for issue in validate_plan(swapped, profile)
        if (issue.code, issue.week, issue.message) not in known
    ]
    return swapped, issues
