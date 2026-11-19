"""Hook for adapting a plan to what was actually done (not implemented yet).

Intended flow, for a future version:
1. read completed activities and wellness from intervals.icu
   (IntervalsClient, GET /athlete/{id}/activities and /wellness),
2. turn them into DayObservation records, then into weeks of synthetic sessions,
3. call replan.replan(plan, profile, today, history=those weeks): the weeks done replace
   the planned ones as the scheduler's history, the progression state is seeded from them
   and the remaining weeks are rebuilt (pinned sessions from amendments are kept), so that
   every ramp cap is computed from the real load instead of the planned one.

The replanning engine (replan.py) and the pins (scheduler.WeekPins) already exist; only the
activity reading and the rules of the roadmap's F8 remain.
"""

import datetime as dt
from dataclasses import dataclass

from endurance_planner.models.enums import Sport
from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile


@dataclass(frozen=True)
class DayObservation:
    """What happened on one day."""

    date: dt.date
    sport: Sport | None = None  # None = rest day or wellness-only record
    duration_s: int = 0
    tss: float = 0.0
    readiness: float | None = None  # e.g. HRV- or sleep-based score, 0-1


def adjust_plan(
    plan: Plan, profile: Profile, observations: list[DayObservation], today: dt.date
) -> Plan:
    """Rebuild the weeks after `today` from the observed training. Not implemented."""
    raise NotImplementedError("auto-adaptation from completed activities is not implemented yet")
