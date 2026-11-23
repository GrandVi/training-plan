"""Planned vs actual, week by week (F7). Pure: a plan, a history and `today` in, a report out.

Per week: planned and actual duration and TSS (total and by category: bike, run, strength,
other), the completion of each key session (actual time of the same sport on the same day
over the planned time), missed key sessions, planned and actual hard sessions, and the real
low/high intensity share when the activities carry time in zones. Actual totals include every
activity, also those of unknown types (reported under `other`, never dropped).
"""

import csv
import datetime as dt
import io
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from endurance_planner.load import strength_tss
from endurance_planner.models.enums import Phase
from endurance_planner.models.history import DayObservation, History
from endurance_planner.models.plan import Plan, Week
from endurance_planner.rules import KEY_COMPLETED_SHARE, REVIEW_HARD_IF, REVIEW_LOW_ZONES
from endurance_planner.units import format_duration

CATEGORIES = ("bike", "run", "strength", "other")
KeyStatus = Literal["done", "partial", "missed", "upcoming"]


class Totals(BaseModel):
    planned_s: int = 0
    actual_s: int = 0
    planned_tss: float = 0.0
    actual_tss: float = 0.0


class KeyReview(BaseModel):
    date: dt.date
    sport: str
    name: str
    planned_s: int
    actual_s: int
    completion: float  # actual / planned time of the same sport that day
    status: KeyStatus


class WeekReview(BaseModel):
    index: int
    start: dt.date
    phase: Phase
    recovery: bool
    finished: bool  # the week is over (before today)
    total: Totals
    by_category: dict[str, Totals]
    keys: list[KeyReview]
    missed_keys: int
    planned_hard: int
    actual_hard: int
    low_share: float | None  # real share of time in the low zones; None without zone times
    other_types: dict[str, int]  # unknown activity types and their counts

    @property
    def completion(self) -> float | None:
        return self.total.actual_s / self.total.planned_s if self.total.planned_s else None


class PlanReview(BaseModel):
    plan_id: str
    today: dt.date
    synced: dt.date | None
    weeks: list[WeekReview]
    outside: int  # activities outside the plan's weeks (not reviewed)


def _hard(observation: DayObservation) -> bool:
    factor = observation.intensity_factor
    return (
        observation.category in ("bike", "run") and factor is not None and factor >= REVIEW_HARD_IF
    )


def _low_share(observations: Sequence[DayObservation]) -> float | None:
    low = total = 0
    for observation in observations:
        if observation.zone_times_s:
            low += sum(observation.zone_times_s[:REVIEW_LOW_ZONES])
            total += sum(observation.zone_times_s)
    return round(low / total, 3) if total else None


def review_week(week: Week, observations: Sequence[DayObservation], today: dt.date) -> WeekReview:
    """One week: `observations` are those of the week's dates."""
    by_category = {category: Totals() for category in CATEGORIES}
    for session in week.sessions:
        totals = by_category[session.sport.value]
        totals.planned_s += session.duration_s
        totals.planned_tss += session.tss
    for strength in week.strength:
        by_category["strength"].planned_s += strength.minutes * 60
        by_category["strength"].planned_tss += strength_tss(strength.minutes)
    other_types: dict[str, int] = {}
    for observation in observations:
        category = observation.category if observation.category in CATEGORIES else "other"
        totals = by_category[category]
        totals.actual_s += observation.duration_s
        totals.actual_tss += observation.tss
        if category == "other":
            name = observation.activity_type or "(no type)"
            other_types[name] = other_types.get(name, 0) + 1
    for totals in by_category.values():
        totals.planned_tss = round(totals.planned_tss, 1)
        totals.actual_tss = round(totals.actual_tss, 1)
    total = Totals(
        planned_s=sum(t.planned_s for t in by_category.values()),
        actual_s=sum(t.actual_s for t in by_category.values()),
        planned_tss=round(sum(t.planned_tss for t in by_category.values()), 1),
        actual_tss=round(sum(t.actual_tss for t in by_category.values()), 1),
    )
    keys: list[KeyReview] = []
    for session in week.sessions:
        if not session.intent.hard:
            continue
        actual = sum(
            o.duration_s
            for o in observations
            if o.date == session.date and o.sport is session.sport
        )
        completion = round(actual / session.duration_s, 2)
        status: KeyStatus
        if session.date >= today and actual == 0:
            status = "upcoming"
        elif completion >= KEY_COMPLETED_SHARE:
            status = "done"
        else:
            status = "partial" if actual else "missed"
        keys.append(
            KeyReview(
                date=session.date,
                sport=session.sport.value,
                name=session.name,
                planned_s=session.duration_s,
                actual_s=actual,
                completion=completion,
                status=status,
            )
        )
    return WeekReview(
        index=week.index,
        start=week.start,
        phase=week.phase,
        recovery=week.recovery,
        finished=week.start + dt.timedelta(days=7) <= today,
        total=total,
        by_category=by_category,
        keys=keys,
        missed_keys=sum(key.status == "missed" for key in keys),
        planned_hard=sum(s.intent.hard for s in week.sessions),
        actual_hard=sum(_hard(o) for o in observations),
        low_share=_low_share(observations),
        other_types=other_types,
    )


def review_plan(
    plan: Plan,
    history: History,
    today: dt.date,
    *,
    week: int | None = None,
    since: dt.date | None = None,
) -> PlanReview:
    """The weeks that have started by `today` (or week `week`, 1-based, or the weeks ending on
    or after `since`), planned vs actual."""
    if week is not None and not 1 <= week <= len(plan.weeks):
        raise ValueError(f"no week {week}: the plan has weeks 1 to {len(plan.weeks)}")
    first, last = plan.weeks[0].start, plan.weeks[-1].start + dt.timedelta(days=6)
    reviews = []
    for item in plan.weeks:
        end = item.start + dt.timedelta(days=6)
        if week is not None and item.index != week:
            continue
        if week is None and (item.start > today or (since is not None and end < since)):
            continue
        reviews.append(review_week(item, history.between(item.start, end), today))
    outside = sum(not first <= o.date <= last for o in history.observations)
    return PlanReview(
        plan_id=plan.id, today=today, synced=history.synced, weeks=reviews, outside=outside
    )


# ---------------------------------------------------------------------- text forms
REVIEW_COLUMNS = [
    "week",
    "start",
    "phase",
    "planned_h",
    "actual_h",
    "planned_tss",
    "actual_tss",
    "bike_h_planned",
    "bike_h_actual",
    "run_h_planned",
    "run_h_actual",
    "strength_min_planned",
    "strength_min_actual",
    "other_h",
    "keys_done",
    "keys",
    "missed_keys",
    "hard_planned",
    "hard_actual",
    "low_pct",
    "other_types",
]


def _hours(seconds: int) -> str:
    return f"{seconds / 3600:.1f}"


def review_row(week: WeekReview) -> dict[str, str]:
    cat = week.by_category
    low = "" if week.low_share is None else f"{week.low_share * 100:.0f}"
    return {
        "week": str(week.index),
        "start": week.start.isoformat(),
        "phase": week.phase.value + (" (rec)" if week.recovery else ""),
        "planned_h": _hours(week.total.planned_s),
        "actual_h": _hours(week.total.actual_s),
        "planned_tss": f"{week.total.planned_tss:.0f}",
        "actual_tss": f"{week.total.actual_tss:.0f}",
        "bike_h_planned": _hours(cat["bike"].planned_s),
        "bike_h_actual": _hours(cat["bike"].actual_s),
        "run_h_planned": _hours(cat["run"].planned_s),
        "run_h_actual": _hours(cat["run"].actual_s),
        "strength_min_planned": str(cat["strength"].planned_s // 60),
        "strength_min_actual": str(cat["strength"].actual_s // 60),
        "other_h": _hours(cat["other"].actual_s),
        "keys_done": str(sum(key.status == "done" for key in week.keys)),
        "keys": str(len(week.keys)),
        "missed_keys": str(week.missed_keys),
        "hard_planned": str(week.planned_hard),
        "hard_actual": str(week.actual_hard),
        "low_pct": low,
        "other_types": "; ".join(f"{name} x{count}" for name, count in week.other_types.items()),
    }


def review_csv(review: PlanReview) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=REVIEW_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for week in review.weeks:
        writer.writerow(review_row(week))
    return buffer.getvalue()


def review_markdown(review: PlanReview) -> str:
    synced = f"last sync {review.synced}" if review.synced else "never synced"
    out = [
        f"# Plan review: {review.plan_id}",
        "",
        f"As of {review.today} ({synced}). Planned includes strength sessions; actual includes "
        "every activity. Keys: done at 80% of the planned time or more, same sport, same day.",
        "",
        "| Wk | Start | Phase | Hours plan/done | TSS plan/done | Bike h | Run h | Strength min "
        "| Keys done | Missed | Hard plan/done | Low % | Other |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for week in review.weeks:
        row = review_row(week)
        other = row["other_types"] or "-"
        out.append(
            f"| {row['week']} | {row['start']} | {row['phase']} "
            f"| {row['planned_h']} / {row['actual_h']} | {row['planned_tss']} / "
            f"{row['actual_tss']} | {row['bike_h_planned']} / {row['bike_h_actual']} "
            f"| {row['run_h_planned']} / {row['run_h_actual']} "
            f"| {row['strength_min_planned']} / {row['strength_min_actual']} "
            f"| {row['keys_done']}/{row['keys']} | {row['missed_keys']} "
            f"| {row['hard_planned']} / {row['hard_actual']} | {row['low_pct'] or '-'} | {other} |"
        )
    out.append("")
    for week in review.weeks:
        if not week.keys:
            continue
        out += [f"## Week {week.index} key sessions", ""]
        for key in week.keys:
            out.append(
                f"- {key.date:%a %d %b} {key.sport} {key.name}: {key.status} "
                f"({format_duration(key.actual_s)} of {format_duration(key.planned_s)}, "
                f"{key.completion:.0%})"
            )
        out.append("")
    if review.outside:
        out += [f"{review.outside} activities are outside the plan's weeks (not reviewed).", ""]
    return "\n".join(out).rstrip() + "\n"
