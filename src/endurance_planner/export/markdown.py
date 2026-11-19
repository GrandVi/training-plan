"""Human-readable summaries: Markdown plan, weekly CSV, CTL/ATL/TSB CSV."""

import csv
import datetime as dt
import io
from collections.abc import Collection

from endurance_planner.load import (
    low_intensity_ratio,
    project_fitness,
    run_impact_multiplier,
    week_weighted_load,
)
from endurance_planner.models.enums import Sport, StepKind
from endurance_planner.models.plan import Plan, Repeat, Session, Step, Week
from endurance_planner.models.profile import CalendarException, Profile
from endurance_planner.rules import baseline_tss
from endurance_planner.thresholds import resolve_thresholds
from endurance_planner.units import format_clock, format_duration
from endurance_planner.zones import ZoneSet, zone_set

__all__ = [
    "daily_tss",
    "fitness_csv",
    "format_duration",
    "render_markdown",
    "step_lines",
    "week_exceptions",
    "week_row",
    "weekly_csv",
]

KIND_PREFIX = {
    StepKind.WARMUP: "Warm-up ",
    StepKind.COOLDOWN: "Cool-down ",
    StepKind.RECOVERY: "Recovery ",
    StepKind.WORK: "",
}
WEEKLY_COLUMNS = [
    "week",
    "start",
    "phase",
    "recovery",
    "hours",
    "bike_hours",
    "run_hours",
    "tss",
    "weighted_load",
    "low_pct",
    "high_pct",
    "key_sessions",
    "races",
]


def step_lines(blocks: list[Step | Repeat], depth: int = 0) -> list[str]:
    """Nested bullet list of steps with relative and absolute targets."""
    pad = "  " * depth
    lines: list[str] = []
    for block in blocks:
        if isinstance(block, Repeat):
            lines.append(f"{pad}- {block.count}x")
            lines.extend(step_lines(block.steps, depth + 1))
            continue
        size = format_duration(block.duration_s)
        if block.distance_m:
            metres = block.distance_m
            distance = f"{metres / 1000:g} km" if metres >= 1000 else f"{metres} m"
            size = f"{distance} (~{size})"
        absolute = block.resolved.label() if block.resolved else ""
        line = f"{pad}- {KIND_PREFIX[block.kind]}{size} @ {block.target.label()}"
        if absolute:
            line += f" ({absolute})"
        if block.note:
            line += f" - {block.note}"
        lines.append(line)
    return lines


def week_row(week: Week, impact: float, zones: ZoneSet) -> dict[str, str]:
    low = low_intensity_ratio(week.sessions, zones)
    hours = {
        sport: sum(s.duration_s for s in week.sessions if s.sport is sport) / 3600
        for sport in Sport
    }
    keys = [s.name for s in week.sessions if s.intent.hard]
    return {
        "week": str(week.index),
        "start": week.start.isoformat(),
        "phase": week.phase.value,
        "recovery": "yes" if week.recovery else "",
        "hours": f"{week.duration_s / 3600:.1f}",
        "bike_hours": f"{hours[Sport.BIKE]:.1f}",
        "run_hours": f"{hours[Sport.RUN]:.1f}",
        "tss": f"{week.tss:.0f}",
        "weighted_load": f"{week_weighted_load(week, impact):.0f}",
        "low_pct": f"{low * 100:.0f}",
        "high_pct": f"{(1 - low) * 100:.0f}",
        "key_sessions": "; ".join(keys),
        "races": "; ".join(week.races),
    }


def weekly_csv(plan: Plan, profile: Profile) -> str:
    impact = run_impact_multiplier(profile)
    zones = zone_set(profile.thresholds.zone_models)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=WEEKLY_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for week in plan.weeks:
        writer.writerow(week_row(week, impact, zones))
    return buffer.getvalue()


def daily_tss(plan: Plan) -> list[tuple[dt.date, float]]:
    by_date = {s.date: s.tss for week in plan.weeks for s in week.sessions}
    days = [week.start + dt.timedelta(days=offset) for week in plan.weeks for offset in range(7)]
    return [(day, by_date.get(day, 0.0)) for day in days]


def fitness_csv(plan: Plan, profile: Profile) -> str:
    """Daily TSS with projected CTL/ATL/TSB (planned sessions only, races excluded).

    CTL and ATL start at the athlete's current average daily TSS.
    """
    days = daily_tss(plan)
    start = baseline_tss(profile) / 7
    points = project_fitness([value for _, value in days], ctl0=start, atl0=start)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "tss", "ctl", "atl", "tsb"])
    for (day, value), point in zip(days, points, strict=True):
        row = [day.isoformat(), f"{value:.1f}", f"{point.ctl:.1f}", f"{point.atl:.1f}"]
        writer.writerow([*row, f"{point.tsb:.1f}"])
    return buffer.getvalue()


def week_exceptions(week: Week, profile: Profile) -> list[CalendarException]:
    """Calendar exceptions that touch the week."""
    last = week.start + dt.timedelta(days=6)
    return [e for e in profile.availability.exceptions if e.overlaps(week.start, last)]


def _session_block(session: Session) -> list[str]:
    tags = [session.slot.value, session.intent.value]
    if session.indoor:
        tags.append("indoor")
    if session.technical:
        tags.append("technical")
    if session.pinned:
        tags.append(f"pinned by {session.amendment_id}")
    lines = [
        f"#### {session.date:%a %d %b} - {session.activity_type.value}: {session.name}",
        "",
        f"{format_duration(session.duration_s)}, {session.tss:.0f} TSS ({', '.join(tags)})",
        "",
        *step_lines(session.steps),
        "",
        f"Why: {session.rationale}",
    ]
    if session.notes:
        lines.append(f"Notes: {session.notes}")
    lines.append("")
    return lines


def _threshold_lines(profile: Profile, today: dt.date) -> list[str]:
    resolved, _ = resolve_thresholds(profile, today)
    estimated = set(resolved.estimated)

    def mark(field: str) -> str:
        return " (estimated)" if field in estimated else ""

    lines: list[str] = []
    if resolved.ftp_w is not None:
        text = f"- Bike: FTP {resolved.ftp_w:.0f} W{mark('ftp_w')}"
        if profile.athlete.weight_kg:
            text += f" ({resolved.ftp_w / profile.athlete.weight_kg:.1f} W/kg)"
        if resolved.bike_lthr_bpm:
            text += f", LTHR {resolved.bike_lthr_bpm} bpm"
        lines.append(text)
    if resolved.threshold_pace_s is not None:
        pace = format_clock(resolved.threshold_pace_s)
        text = f"- Run: threshold pace {pace} /km{mark('threshold_pace_s')}"
        if resolved.run_lthr_bpm:
            text += f", LTHR {resolved.run_lthr_bpm} bpm"
        lines.append(text)
    return lines


def render_markdown(
    plan: Plan,
    profile: Profile,
    weeks: Collection[int] | None = None,
    details: bool = True,
) -> str:
    """Markdown summary: goals, thresholds, warnings, weekly table, then session details.

    weeks: 1-based week numbers to detail (default: all). details=False keeps the table only.
    """
    from endurance_planner.amend import amendment_lines  # amend -> replan -> planner

    profile = plan.effective_profile(profile)
    impact = run_impact_multiplier(profile)
    zones = zone_set(profile.thresholds.zone_models)
    last = plan.weeks[-1].start + dt.timedelta(days=6)
    out = [
        f"# Training plan: {plan.athlete}",
        "",
        f"Plan `{plan.id}`, created {plan.created}, {len(plan.weeks)} weeks "
        f"({plan.weeks[0].start} to {last}).",
        "",
        "## Goals",
        "",
    ]
    for goal in plan.goals:
        target = ""
        if goal.target.time:
            target = f", target {format_clock(goal.target.time)}"
        elif goal.target.duration_h:
            target = f", about {goal.target.duration_h:g} h"
        out.append(f"- {goal.date} [{goal.priority.value}] {goal.name} ({goal.type.value}{target})")
    out += ["", "## Thresholds", "", *_threshold_lines(profile, plan.created), ""]
    if plan.warnings:
        out += ["## Warnings", "", *[f"- {issue}" for issue in plan.warnings], ""]
    if plan.amendments:
        out += ["## Amendments", "", *[f"- {line}" for line in amendment_lines(plan)], ""]

    ratio = profile.preferences.intensity_ratio
    out += [
        "## Weekly overview",
        "",
        f"Load = TSS with run TSS x {impact:g} (used for ramp limits). Low/high = share of time "
        f"below/above LT1, both sports combined (target {ratio.low:.0%}/{ratio.high:.0%}).",
        "",
        "| Wk | Start | Phase | Hours | Bike | Run | TSS | Load | Low/High | Key sessions |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for week in plan.weeks:
        row = week_row(week, impact, zones)
        phase = row["phase"] + (" (rec)" if week.recovery else "")
        phase += " \\*" if week_exceptions(week, profile) else ""
        keys = row["key_sessions"] or "-"
        if week.races:
            keys += f" / RACE: {row['races']}"
        out.append(
            f"| {row['week']} | {row['start']} | {phase} | {row['hours']} | {row['bike_hours']} "
            f"| {row['run_hours']} | {row['tss']} | {row['weighted_load']} "
            f"| {row['low_pct']}/{row['high_pct']} | {keys} |"
        )
    out.append("")
    if any(week_exceptions(week, profile) for week in plan.weeks):
        out += ["\\* calendar exception this week (see the week details)", ""]
    if details:
        for week in plan.weeks:
            if weeks is not None and week.index not in weeks:
                continue
            label = "recovery week" if week.recovery else week.phase.value
            out += [f"## Week {week.index} - {label} ({week.start})", ""]
            if week.races:
                out += [f"Race this week: {', '.join(week.races)}", ""]
            for exception in week_exceptions(week, profile):
                out += [f"Calendar exception: {exception.label()}", ""]
            for session in week.sessions:
                out += _session_block(session)
    return "\n".join(out).rstrip() + "\n"
