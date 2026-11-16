"""Relative targets -> absolute values (watts, pace, HR) for given thresholds.

Resolution never changes the plan structure or the relative targets, so it can
be re-run after a threshold update (retargeting).
"""

from endurance_planner.models.enums import Sport, TargetKind
from endurance_planner.models.plan import Plan, Repeat, ResolvedTarget, Session, Step, Target
from endurance_planner.models.profile import ZoneModel
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.zones import if_to_hr, if_to_rpe, rpe_to_if

# No HR guide above this intensity: HR lags and does not plateau on short hard efforts.
HR_GUIDE_MAX_IF = 1.06
# Width given to open-ended zones when a whole zone is used as a target.
BOTTOM_ZONE_SPAN = 0.8
TOP_ZONE_SPAN = 1.1

NATIVE_KIND = {Sport.BIKE: TargetKind.POWER, Sport.RUN: TargetKind.PACE}


def zone_target(model: ZoneModel, kind: TargetKind, zone: int) -> Target:
    """Target spanning a whole zone (1-based), for sessions planned by zone only."""
    upper = model.zones[zone - 1].upper
    lower = model.zones[zone - 2].upper if zone > 1 else None
    if lower is None:
        assert upper is not None
        lower = BOTTOM_ZONE_SPAN * upper
    if upper is None:
        upper = TOP_ZONE_SPAN * lower
    return Target(kind=kind, low=round(lower, 4), high=round(upper, 4))


def express_target(target: Target, sport: Sport, mode: str) -> Target:
    """Re-express a native (power/pace) target in the athlete's target mode.

    'hr' falls back to RPE above HR_GUIDE_MAX_IF, where HR is not a usable target.
    """
    if target.kind is not NATIVE_KIND[sport] or mode == target.kind.value:
        return target
    if mode == "hr" and target.high <= HR_GUIDE_MAX_IF:
        return Target(
            kind=TargetKind.HR,
            low=round(if_to_hr(target.low, sport), 2),
            high=round(if_to_hr(target.high, sport), 2),
        )
    return Target(
        kind=TargetKind.RPE,
        low=round(if_to_rpe(target.low, sport) * 2) / 2,
        high=round(if_to_rpe(target.high, sport) * 2) / 2,
    )


def express_blocks(blocks: list[Step | Repeat], sport: Sport, mode: str) -> list[Step | Repeat]:
    out: list[Step | Repeat] = []
    for block in blocks:
        if isinstance(block, Repeat):
            steps = express_blocks(block.steps, sport, mode)
            out.append(block.model_copy(update={"steps": steps}))
        else:
            target = express_target(block.target, sport, mode)
            out.append(block.model_copy(update={"target": target}))
    return out


def resolve_target(target: Target, sport: Sport, thresholds: ResolvedThresholds) -> ResolvedTarget:
    """Absolute values for a target; fields stay None when the threshold is unknown."""
    power = pace = hr = None
    lthr = thresholds.lthr(sport)
    intensity: tuple[float, float] | None = (target.low, target.high)
    match target.kind:
        case TargetKind.POWER:
            if thresholds.ftp_w is not None:
                power = (
                    round(target.low * thresholds.ftp_w),
                    round(target.high * thresholds.ftp_w),
                )
        case TargetKind.PACE:
            if thresholds.threshold_pace_s is not None:
                pace = (
                    round(thresholds.threshold_pace_s / target.low),
                    round(thresholds.threshold_pace_s / target.high),
                )
        case TargetKind.HR:
            intensity = None
            if lthr is not None:
                hr = (round(target.low * lthr), round(target.high * lthr))
        case TargetKind.RPE:
            intensity = (rpe_to_if(target.low, sport), rpe_to_if(target.high, sport))
    if intensity is not None and lthr is not None and intensity[1] <= HR_GUIDE_MAX_IF:
        hr = (
            round(if_to_hr(intensity[0], sport) * lthr),
            round(if_to_hr(intensity[1], sport) * lthr),
        )
    return ResolvedTarget(power_w=power, pace_s_per_km=pace, hr_bpm=hr)


def _resolve_step(step: Step, sport: Sport, thresholds: ResolvedThresholds) -> Step:
    update: dict[str, object] = {"resolved": resolve_target(step.target, sport, thresholds)}
    pace = thresholds.threshold_pace_s
    if step.distance_m is not None and step.target.kind is TargetKind.PACE and pace is not None:
        update["duration_s"] = round(step.distance_m / 1000 * pace / step.target.mid)
    return step.model_copy(update=update)


def resolve_blocks(
    blocks: list[Step | Repeat], sport: Sport, thresholds: ResolvedThresholds
) -> list[Step | Repeat]:
    resolved: list[Step | Repeat] = []
    for block in blocks:
        if isinstance(block, Repeat):
            steps = resolve_blocks(block.steps, sport, thresholds)
            resolved.append(block.model_copy(update={"steps": steps}))
        else:
            resolved.append(_resolve_step(block, sport, thresholds))
    return resolved


def resolve_session(session: Session, thresholds: ResolvedThresholds) -> Session:
    """Copy of the session with all absolute targets recomputed."""
    steps = resolve_blocks(session.steps, session.sport, thresholds)
    return session.model_copy(update={"steps": steps})


def resolve_plan(plan: Plan, thresholds: ResolvedThresholds) -> Plan:
    """Retarget: recompute every absolute target; structure, dates and load are untouched."""
    weeks = [
        week.model_copy(
            update={"sessions": [resolve_session(s, thresholds) for s in week.sessions]}
        )
        for week in plan.weeks
    ]
    return plan.model_copy(update={"weeks": weeks})
