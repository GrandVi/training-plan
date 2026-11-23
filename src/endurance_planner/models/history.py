"""What was actually done (F7): one DayObservation per completed activity, one WellnessDay
per day with wellness data, kept in history.json (see history.py for reading them from
intervals.icu and merging)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from pydantic import BaseModel, Field

from endurance_planner.models.enums import Sport

# intervals.icu activity type -> category. Bike and run map to a Sport; strength stays apart
# (it is not a Sport); any other type is "other": stored, counted and reported, never dropped.
ACTIVITY_CATEGORY: dict[str, str] = {
    "Ride": "bike",
    "VirtualRide": "bike",
    "GravelRide": "bike",
    "MountainBikeRide": "bike",
    "EBikeRide": "bike",
    "Run": "run",
    "TrailRun": "run",
    "VirtualRun": "run",
    "WeightTraining": "strength",
}
CATEGORY_SPORT = {"bike": Sport.BIKE, "run": Sport.RUN}


@dataclass(frozen=True)
class DayObservation:
    """What happened on one day: one completed activity (or a wellness-only record)."""

    date: dt.date
    sport: Sport | None = None  # None: strength, another activity, or no activity
    duration_s: int = 0
    tss: float = 0.0
    readiness: float | None = None  # e.g. HRV- or sleep-based score, 0-1
    # F7. Activity identity and intensity.
    activity_id: str | None = None
    intensity_factor: float | None = None
    zone_times_s: tuple[int, ...] | None = None  # time in zones, lowest zone first
    source: str = "intervals.icu"
    activity_type: str = ""  # type as given by the source (Ride, Run, WeightTraining, Swim...)
    name: str = ""
    tss_estimated: bool = False  # no load given by the source: computed from duration and IF

    @property
    def category(self) -> str:
        """bike, run, strength, other (an unknown activity type) or rest (no activity)."""
        if self.sport is not None:
            return self.sport.value
        if self.activity_type:
            return ACTIVITY_CATEGORY.get(self.activity_type, "other")
        return "rest" if self.activity_id is None else "other"


@dataclass(frozen=True)
class WellnessDay:
    """Wellness values of one day; every value is optional (field names UNVERIFIED, see
    history.WELLNESS_FIELDS)."""

    date: dt.date
    hrv: float | None = None  # ms
    resting_hr: float | None = None  # bpm
    sleep_s: int | None = None
    sleep_score: float | None = None
    ctl: float | None = None
    atl: float | None = None


HISTORY_SCHEMA_VERSION = 1


class History(BaseModel):
    """Contents of history.json: observations sorted by date then activity id, wellness by
    date."""

    schema_version: int = HISTORY_SCHEMA_VERSION
    synced: dt.date | None = None  # day of the last sync
    observations: list[DayObservation] = Field(default_factory=list)
    wellness: list[WellnessDay] = Field(default_factory=list)

    def between(self, first: dt.date, last: dt.date) -> list[DayObservation]:
        return [o for o in self.observations if first <= o.date <= last]
