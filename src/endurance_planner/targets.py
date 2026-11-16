"""Relative targets -> absolute values (watts, pace, HR) for given thresholds.

Resolution never changes the plan structure or the relative targets, so it can
be re-run after a threshold update (retargeting).
"""

from endurance_planner.models.enums import Sport, TargetKind
from endurance_planner.models.plan import Repeat, ResolvedTarget, Session, Step, Target
from endurance_planner.models.profile import ZoneModel
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.zones import if_to_hr, rpe_to_if

# No HR guide above this intensity: HR lags and does not plateau on short hard efforts.
HR_GUIDE_MAX_IF = 1.06
# Width given to open-ended zones when a whole zone is used as a target.
BOTTOM_ZONE_SPAN = 0.8
TOP_ZONE_SPAN = 1.1


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
            intensity = (rpe_to_if(target.low), rpe_to_if(target.high))
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
