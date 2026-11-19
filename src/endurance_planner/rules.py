"""Numeric planning rules, shared by the scheduler (to stay under the caps) and
the validator (to check them). Tune the constants here; per-athlete caps live in
profile.preferences.limits.
"""

import datetime as dt
from collections.abc import Sequence
from typing import NamedTuple

from endurance_planner.load import run_impact_multiplier, tss
from endurance_planner.models.enums import Intent, Scope, Sport
from endurance_planner.models.plan import Week
from endurance_planner.models.profile import Availability, Profile

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

# Return from a break (consecutive days on which no training is possible): load cap of the
# first weeks back, as a fraction of the reference load (highest of the last 4 weeks).
# Rows are (minimum days without training, caps of the 1st, 2nd... week back), longest first.
RETURN_FROM_BREAK: tuple[tuple[int, tuple[float, ...]], ...] = (
    (14, (0.70, 0.85)),
    (7, (0.85,)),
)
# A calendar exception in the last days before an A race gets a warning.
PRE_RACE_EXCEPTION_DAYS = 10

# --- amendments of a running plan (amend.py) -------------------------------------------
# Default reach of an amendment: the amended week and every later week are rebuilt.
DEFAULT_SCOPE = Scope.REST
# A key session done at this share of its planned length (or more) completes its rung;
# skipped or shorter, its ladder does not advance and the next one repeats the rung.
KEY_COMPLETED_SHARE = 0.80
# Below the first rung of its ladder, a key session's main set may be scaled down to this
# share of its length at most; shorter than that, it becomes plain endurance (with a warning).
KEY_MIN_SCALE = 0.75
# `--intent` only makes a session easier.
EASIER_INTENTS = (Intent.ENDURANCE, Intent.RECOVERY)
# Lost load (skipped or shortened sessions) is never made up by other sessions: the rest of
# the week is built with the lost volume, load and run time taken off its targets.
# Unpinned sessions yield to amended ones: when a pinned session takes a day, breaks the
# 48 h spacing or the weekly hard-session limit, the other key session moves to a free day,
# else is dropped to an easy session; when the week's load is over its cap, the other key
# sessions go down their ladders and the easy volume shrinks.

# Reason codes of the changes listed after an amendment or a replan.
CHANGE_REASONS = {
    "amended": "the session you changed (pinned)",
    "skipped": "the session you skipped",
    "moved": "the session moved away from this day",
    "yielded_to_amendment": "same week as an amendment: moved, eased or resized to keep the "
    "week within its rules",
    "rung_held": "next session of a skipped or shortened key session: the rung is repeated",
    "rebalanced": "later week rebuilt: ramp references and ladder progress changed",
    "calendar_exception": "date covered by a calendar exception or an unavailable range",
    "undone": "amendment undone",
    "replanned": "rebuilt from the current profile",
}


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


class BreakReturn(NamedTuple):
    cap: float  # max load of the week, as a fraction of the reference load
    week: int  # 1 = first week back, 2 = second week back
    gap_days: int  # length of the break


def return_caps(gap_days: int) -> tuple[float, ...]:
    """Load caps of the weeks back after `gap_days` days without training (may be empty)."""
    for days, caps in RETURN_FROM_BREAK:
        if gap_days >= days:
            return caps
    return ()


def training_possible(availability: Availability, date: dt.date) -> bool:
    return any(availability.minutes(date, sport) > 0 for sport in Sport)


def days_without_training(availability: Availability, day: dt.date, since: dt.date) -> int:
    """Consecutive days before `day`, back to `since` at most, with no training possible."""
    count = 0
    date = day - dt.timedelta(days=1)
    while date >= since and not training_possible(availability, date):
        count += 1
        date -= dt.timedelta(days=1)
    return count


def break_return(
    availability: Availability, week_start: dt.date, plan_start: dt.date
) -> BreakReturn | None:
    """Return-from-break cap of the week starting on `week_start`, if it applies.

    The first week back is the week of the first day with training possible after the
    break. Days before the plan start do not count. Without calendar exceptions this never
    applies: every week of the weekly pattern has at least one training day.
    """
    depth = max(len(caps) for _, caps in RETURN_FROM_BREAK)
    for back in range(depth):
        start = week_start - dt.timedelta(weeks=back)
        if start < plan_start:
            return None
        days = [start + dt.timedelta(days=offset) for offset in range(7)]
        first = next((day for day in days if training_possible(availability, day)), None)
        if first is None:
            return None
        gap = days_without_training(availability, first, plan_start)
        caps = return_caps(gap)
        if back < len(caps):
            return BreakReturn(caps[back], back + 1, gap)
    return None
