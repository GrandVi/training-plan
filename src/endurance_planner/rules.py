"""Numeric planning rules, shared by the scheduler (to stay under the caps) and
the validator (to check them). Tune the constants here; per-athlete caps live in
profile.preferences.limits.
"""

from collections.abc import Sequence

from endurance_planner.load import run_impact_multiplier, tss
from endurance_planner.models.enums import Sport
from endurance_planner.models.plan import Week
from endurance_planner.models.profile import Profile

# Ramp caps compare a week with the highest value of the previous RAMP_WINDOW weeks,
# so the week after a recovery week is compared with the last loading weeks.
RAMP_WINDOW = 4
# Absolute weekly increases that are always allowed (so that small values can grow).
LOAD_STEP_TSS = 15.0
RUN_STEP_S = 10 * 60
LONG_STEP_S = 5 * 60
# Floors used when the athlete reports no recent training.
STARTER_LOAD_TSS = 100.0
STARTER_VOLUME_S = 2 * 3600
RUN_BOOTSTRAP_S = 40 * 60
LONG_START_S = {Sport.BIKE: 90 * 60, Sport.RUN: 40 * 60}
# Assumed average intensity of the athlete's current training (for the starting load).
BASELINE_IF = {Sport.BIKE: 0.70, Sport.RUN: 0.80}

# Tolerances on the high-intensity share of training time (above the target share).
INTENSITY_TOLERANCE_WEEK = 0.10
INTENSITY_TOLERANCE_MESOCYCLE = 0.05
# A recovery week must be at most this fraction of the previous week's load.
RECOVERY_MAX_RATIO = 0.80
# A-race week volume must be at most this fraction of the highest of the 4 previous weeks.
TAPER_MAX_RATIO = 0.75
# Minimum days between two hard sessions (2 = at least 48 h, never on consecutive days).
HARD_MIN_GAP_DAYS = 2


def reference(history: Sequence[float], before: float) -> float:
    """Highest value of the last RAMP_WINDOW weeks; `before` stands for pre-plan weeks."""
    padded = [before] * RAMP_WINDOW + list(history)
    return max(padded[-RAMP_WINDOW:])


def baseline_load(profile: Profile) -> float:
    """Impact-weighted weekly load of the athlete's current training."""
    hours = profile.current_fitness.weekly_hours
    load = tss(hours.bike * 3600, BASELINE_IF[Sport.BIKE])
    load += tss(hours.run * 3600, BASELINE_IF[Sport.RUN]) * run_impact_multiplier(profile)
    return max(load, STARTER_LOAD_TSS)


def baseline_tss(profile: Profile) -> float:
    """Plain (unweighted) weekly TSS of the athlete's current training."""
    hours = profile.current_fitness.weekly_hours
    return sum(tss(hours.of(sport) * 3600, BASELINE_IF[sport]) for sport in Sport)


def current_run_s(profile: Profile) -> float:
    return profile.current_fitness.weekly_hours.run * 3600


def longest_recent_s(profile: Profile, sport: Sport) -> float:
    return profile.current_fitness.longest_recent_session_min.of(sport) * 60


def load_cap(history: Sequence[float], profile: Profile) -> float:
    """Max impact-weighted load of the next week given previous weekly loads."""
    ref = reference(history, baseline_load(profile))
    return max(ref * (1 + profile.preferences.limits.max_weekly_ramp), ref + LOAD_STEP_TSS)


def run_volume_cap(history_s: Sequence[float], profile: Profile) -> float:
    """Max run seconds of the next week given previous weekly run volumes."""
    ref = reference(history_s, current_run_s(profile))
    ramp = profile.preferences.limits.max_run_volume_increase
    return max(ref * (1 + ramp), ref + RUN_STEP_S, RUN_BOOTSTRAP_S)


def longest_session_cap(history_s: Sequence[float], sport: Sport, profile: Profile) -> float:
    """Max duration of any session of `sport` next week given previous weekly longest sessions."""
    ref = reference(history_s, longest_recent_s(profile, sport))
    ramp = profile.preferences.limits.max_long_session_increase
    return max(ref * (1 + ramp), ref + LONG_STEP_S, LONG_START_S[sport])


def week_run_s(week: Week) -> int:
    return sum(s.duration_s for s in week.sessions if s.sport is Sport.RUN)


def week_longest_s(week: Week, sport: Sport) -> int:
    return max((s.duration_s for s in week.sessions if s.sport is sport), default=0)
