"""Training load model.

Planned TSS of a step = hours * IF^2 * 100, where IF is the step's intensity
factor (fraction of FTP for the bike, of threshold speed for the run: rTSS).
HR and RPE targets are mapped to an IF first (hrTSS-style fallback).
The load depends only on relative targets, never on absolute thresholds.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, NamedTuple

from endurance_planner.models.enums import Sport, TargetKind
from endurance_planner.zones import (
    DEFAULT_ZONES,
    RPE_LOW_MAX,
    ZoneSet,
    hr_to_if,
    low_ceiling,
    rpe_to_if,
)

if TYPE_CHECKING:
    from endurance_planner.models.plan import Session, Step, Target, Week
    from endurance_planner.models.profile import Profile


def tss(duration_s: float, intensity_factor: float) -> float:
    return duration_s / 3600 * intensity_factor**2 * 100


def intensity_factor(target: Target, sport: Sport) -> float:
    match target.kind:
        case TargetKind.HR:
            return hr_to_if(target.mid, sport)
        case TargetKind.RPE:
            return rpe_to_if(target.mid, sport)
        case _:
            return target.mid


def steps_tss(steps: Iterable[Step], sport: Sport) -> float:
    """TSS of flattened steps (repeats already expanded)."""
    return sum(tss(step.duration_s, intensity_factor(step.target, sport)) for step in steps)


def is_low_intensity(target: Target, sport: Sport, zones: ZoneSet = DEFAULT_ZONES) -> bool:
    """True if the target midpoint is below LT1 (within the model's low zones)."""
    if target.kind is TargetKind.RPE:
        return target.mid <= RPE_LOW_MAX
    return target.mid <= low_ceiling(zones.model(sport, target.kind)) + 1e-9


def intensity_split(session: Session, zones: ZoneSet = DEFAULT_ZONES) -> tuple[int, int]:
    """(low, high) seconds by time in zone; recoveries between efforts count as low."""
    low = high = 0
    for step in session.flat_steps():
        if is_low_intensity(step.target, session.sport, zones):
            low += step.duration_s
        else:
            high += step.duration_s
    return low, high


def low_intensity_ratio(sessions: Iterable[Session], zones: ZoneSet = DEFAULT_ZONES) -> float:
    """Share of total time spent at low intensity, across both sports."""
    low = total = 0
    for session in sessions:
        session_low, session_high = intensity_split(session, zones)
        low += session_low
        total += session_low + session_high
    return low / total if total else 1.0


def run_impact_multiplier(profile: Profile) -> float:
    limits = profile.preferences.limits
    bonus = limits.injury_run_impact_bonus if profile.athlete.injury_notes.strip() else 0.0
    return limits.run_impact_multiplier + bonus


def weighted_load(session: Session, run_impact: float) -> float:
    """Load used for ramp/fatigue limits: run TSS is scaled by the impact multiplier."""
    return session.tss * (run_impact if session.sport is Sport.RUN else 1.0)


def week_weighted_load(week: Week, run_impact: float) -> float:
    return sum(weighted_load(session, run_impact) for session in week.sessions)


class FitnessPoint(NamedTuple):
    ctl: float
    atl: float
    tsb: float


def project_fitness(
    daily_tss: Sequence[float],
    ctl0: float = 0.0,
    atl0: float = 0.0,
    ctl_days: int = 42,
    atl_days: int = 7,
) -> list[FitnessPoint]:
    """CTL/ATL as exponentially weighted averages of daily TSS; TSB = yesterday's CTL - ATL."""
    points: list[FitnessPoint] = []
    ctl, atl = ctl0, atl0
    for day_tss in daily_tss:
        tsb = ctl - atl
        ctl += (day_tss - ctl) / ctl_days
        atl += (day_tss - atl) / atl_days
        points.append(FitnessPoint(ctl, atl, tsb))
    return points
