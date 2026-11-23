"""Completed activities and wellness from intervals.icu, as history.json (F7). Pure.

parse_activity / parse_wellness read one record and never fail on missing or unknown
fields (the record is skipped only when it has no id or no date). merge_history is
idempotent by activity id: the same data merged twice changes nothing, newer data for an
activity replaces it, and observations no longer returned are kept.

Field names. Activities (documented by intervals.icu): id, type, start_date_local,
moving_time (fallback elapsed_time), icu_training_load, icu_intensity (IF in %), name.
UNVERIFIED (no `doctor` run yet): time in zones as icu_zone_times (power, a list of
{"id", "secs"}), pace_zone_times and icu_hr_zone_times (lists of seconds); wellness as id or
date, hrv, restingHR, sleepSecs, sleepScore, ctl, atl (see WELLNESS_FIELDS).
"""

import datetime as dt
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from endurance_planner.load import strength_tss, tss
from endurance_planner.models.history import (
    ACTIVITY_CATEGORY,
    CATEGORY_SPORT,
    DayObservation,
    History,
    WellnessDay,
)
from endurance_planner.rules import ACTIVITY_DEFAULT_IF, SYNC_FIRST_DAYS, SYNC_OVERLAP_DAYS

# WellnessDay field -> names accepted in an intervals.icu wellness record (UNVERIFIED).
WELLNESS_FIELDS: dict[str, tuple[str, ...]] = {
    "hrv": ("hrv", "hrvRMSSD"),
    "resting_hr": ("restingHR", "resting_hr"),
    "sleep_s": ("sleepSecs", "sleep_secs"),
    "sleep_score": ("sleepScore", "sleep_score"),
    "ctl": ("ctl",),
    "atl": ("atl",),
}
# Time in zones by category, first name found wins (UNVERIFIED names).
ZONE_FIELDS: dict[str, tuple[str, ...]] = {
    "bike": ("icu_zone_times", "icu_hr_zone_times"),
    "run": ("pace_zone_times", "icu_hr_zone_times"),
    "other": ("icu_hr_zone_times",),
}


def _number(value: Any) -> float | None:
    """A finite, non-negative number, else None (strings and booleans are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _zone_times(record: Mapping[str, Any], category: str) -> tuple[int, ...] | None:
    for name in ZONE_FIELDS.get(category, ()):
        value = record.get(name)
        if not isinstance(value, list) or not value:
            continue
        seconds: list[int] = []
        for item in value:
            raw = item.get("secs") if isinstance(item, dict) else item
            number = _number(raw)
            if number is None:
                break
            seconds.append(int(number))
        else:
            return tuple(seconds)
    return None


def parse_activity(record: Mapping[str, Any]) -> DayObservation | None:
    """One intervals.icu activity as a DayObservation; None without an id or a date."""
    activity_id = record.get("id")
    date = _date(record.get("start_date_local"))
    if activity_id in (None, "") or date is None:
        return None
    activity_type = str(record.get("type") or "")
    category = ACTIVITY_CATEGORY.get(activity_type, "other")
    duration = _number(record.get("moving_time"))
    if duration is None:
        duration = _number(record.get("elapsed_time")) or 0.0
    load = _number(record.get("icu_training_load"))
    intensity = _number(record.get("icu_intensity"))
    factor = intensity / 100 if intensity is not None and intensity > 3 else intensity
    estimated = load is None
    if load is None:
        if category == "strength":
            load = strength_tss(duration / 60)
        else:
            load = tss(duration, factor or ACTIVITY_DEFAULT_IF.get(category, 0.6))
    if factor is None and category != "strength" and duration > 0 and load > 0:
        factor = math.sqrt(load / (duration / 3600 * 100))
    return DayObservation(
        date=date,
        sport=CATEGORY_SPORT.get(category),
        duration_s=int(duration),
        tss=round(load, 1),
        activity_id=str(activity_id),
        intensity_factor=round(factor, 3) if factor is not None else None,
        zone_times_s=_zone_times(record, category),
        source="intervals.icu",
        activity_type=activity_type,
        name=str(record.get("name") or ""),
        tss_estimated=estimated,
    )


def parse_wellness(record: Mapping[str, Any]) -> WellnessDay | None:
    """One wellness record; unknown or unreadable values are ignored. None without a date."""
    date = _date(record.get("id")) or _date(record.get("date"))
    if date is None:
        return None
    values: dict[str, Any] = {}
    for name, keys in WELLNESS_FIELDS.items():
        number = next((n for k in keys if (n := _number(record.get(k))) is not None), None)
        if number is not None:
            values[name] = int(number) if name == "sleep_s" else number
    return WellnessDay(date=date, **values)


@dataclass
class MergeCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    wellness: int = 0  # wellness days added or changed


def merge_history(
    history: History,
    observations: Iterable[DayObservation],
    wellness: Iterable[WellnessDay],
    synced: dt.date | None = None,
) -> tuple[History, MergeCounts]:
    """History with the new data merged (idempotent; see the module docstring)."""
    counts = MergeCounts()
    by_id = {o.activity_id or f"{o.date}:{i}": o for i, o in enumerate(history.observations)}
    for observation in observations:
        key = observation.activity_id or f"{observation.date}:new"
        before = by_id.get(key)
        if before is None:
            counts.added += 1
        elif before != observation:
            counts.updated += 1
        else:
            counts.unchanged += 1
        by_id[key] = observation
    days = {day.date: day for day in history.wellness}
    for day in wellness:
        if days.get(day.date) != day:
            counts.wellness += 1
        days[day.date] = day
    merged = History(
        synced=synced or history.synced,
        observations=sorted(by_id.values(), key=lambda o: (o.date, o.activity_id or "")),
        wellness=[days[date] for date in sorted(days)],
    )
    return merged, counts


def sync_window(history: History, today: dt.date, since: dt.date | None = None) -> dt.date:
    """First day to read: `since`, else the last synced day minus SYNC_OVERLAP_DAYS, else
    SYNC_FIRST_DAYS before today."""
    if since is not None:
        return since
    if history.synced is not None:
        return min(history.synced, today) - dt.timedelta(days=SYNC_OVERLAP_DAYS)
    return today - dt.timedelta(days=SYNC_FIRST_DAYS)


@dataclass
class ParsedRecords:
    observations: list[DayObservation] = field(default_factory=list)
    wellness: list[WellnessDay] = field(default_factory=list)
    unreadable: int = 0  # records without an id or a date
    categories: Counter[str] = field(default_factory=Counter)
    unknown_types: Counter[str] = field(default_factory=Counter)


def parse_records(
    activities: Iterable[Mapping[str, Any]], wellness: Iterable[Mapping[str, Any]]
) -> ParsedRecords:
    """Every activity and wellness record read; unknown activity types are counted."""
    parsed = ParsedRecords()
    for record in activities:
        observation = parse_activity(record) if isinstance(record, Mapping) else None
        if observation is None:
            parsed.unreadable += 1
            continue
        parsed.observations.append(observation)
        parsed.categories[observation.category] += 1
        if observation.category == "other":
            parsed.unknown_types[observation.activity_type or "(no type)"] += 1
    for record in wellness:
        day = parse_wellness(record) if isinstance(record, Mapping) else None
        if day is None:
            parsed.unreadable += 1
            continue
        parsed.wellness.append(day)
    return parsed
