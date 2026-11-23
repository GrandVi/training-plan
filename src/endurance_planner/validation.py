"""Plan validation: every rule of the planner, checked on the finished plan.

validate_plan() returns structured issues; assert_valid() raises on errors.
The numeric rules are shared with the scheduler through rules.py.
"""

import datetime as dt
from collections.abc import Iterator
from itertools import pairwise

from endurance_planner.load import intensity_split, run_impact_multiplier, week_weighted_load
from endurance_planner.models.enums import (
    ActivityType,
    AmendmentKind,
    BikeActivity,
    Equipment,
    GoalType,
    Phase,
    Severity,
    Slot,
    Sport,
    TargetKind,
    Weekday,
)
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Plan, Session, StrengthSession, Week
from endurance_planner.models.profile import Profile
from endurance_planner.rules import (
    HARD_MIN_GAP_DAYS,
    INTENSITY_TOLERANCE_MESOCYCLE,
    INTENSITY_TOLERANCE_WEEK,
    RECOVERY_MAX_RATIO,
    TAPER_MAX_RATIO,
    baseline_load,
    break_return,
    load_cap,
    longest_session_cap,
    reference,
    run_volume_cap,
    week_longest_s,
    week_run_s,
)
from endurance_planner.strength import (
    DAY_PROBLEMS,
    EXERCISES,
    PHASE_TEMPLATE,
    STRENGTH_MIN_GAP_DAYS,
    day_problems,
    exercise_equipment,
    prescription,
    training_days,
    weekly_count,
)
from endurance_planner.strength import (
    TEMPLATES as STRENGTH_TEMPLATES,
)
from endurance_planner.templates import progress_rung
from endurance_planner.zones import ZoneSet, zone_set

WEEKDAYS = list(Weekday)
LONG_SLOTS = (Slot.LONG, Slot.LONG_2)
LOAD_TOLERANCE_TSS = 0.5
TIME_TOLERANCE_S = 60
MIN_HIGH_SHARE = 0.05  # build/peak loading weeks below this get a warning


class PlanValidationError(Exception):
    """The plan breaks at least one hard rule."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("invalid plan:\n" + "\n".join(f"  {issue}" for issue in issues))


def _error(code: str, message: str, week: Week | None = None) -> Issue:
    return Issue(
        severity=Severity.ERROR, code=code, message=message, week=week.index if week else None
    )


def _warning(code: str, message: str, week: Week | None = None) -> Issue:
    return Issue(
        severity=Severity.WARNING, code=code, message=message, week=week.index if week else None
    )


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min"


def _check_structure(plan: Plan, today: dt.date) -> Iterator[Issue]:
    race_dates = {goal.date for goal in plan.goals}
    for previous, week in pairwise([None, *plan.weeks]):
        if week is None:
            continue
        if week.start.weekday() != 0:
            yield _error("week_start", f"week starts on {week.start}, not a Monday", week)
        if previous is not None and week.start != previous.start + dt.timedelta(weeks=1):
            yield _error(
                "weeks_not_contiguous", f"week {week.start} does not follow the previous one", week
            )
        dates = [session.date for session in week.sessions]
        if len(set(dates)) != len(dates):
            yield _error("two_sessions_same_day", "more than one session on the same day", week)
        for session in week.sessions:
            if not week.start <= session.date < week.start + dt.timedelta(weeks=1):
                yield _error("session_outside_week", f"{session.date} is outside its week", week)
            if session.date < today:
                yield _error(
                    "session_in_past", f"{session.date} {session.name} is in the past", week
                )
            if session.date in race_dates:
                yield _error(
                    "session_on_race_day", f"{session.date} {session.name} is on a race day", week
                )
        strength_dates = [session.date for session in week.strength]
        if len(set(strength_dates)) != len(strength_dates):
            yield _error(
                "two_sessions_same_day", "more than one strength session on the same day", week
            )
        for strength in week.strength:
            if not week.start <= strength.date < week.start + dt.timedelta(weeks=1):
                yield _error("session_outside_week", f"{strength.date} is outside its week", week)
            if strength.date < today:
                yield _error(
                    "session_in_past", f"{strength.date} {strength.name} is in the past", week
                )


def _check_availability(plan: Plan, profile: Profile) -> Iterator[Issue]:
    availability = profile.availability
    budget_s = availability.weekly_hours_budget * 3600
    for week in plan.weeks:
        planned_s = week.duration_s + week.strength_minutes * 60  # strength counts too
        if planned_s > budget_s + 1:
            yield _error(
                "weekly_budget",
                f"{planned_s / 3600:.1f} h planned, budget is {budget_s / 3600:.1f} h",
                week,
            )
        if len(week.sessions) > availability.days_per_week:
            yield _error(
                "training_days",
                f"{len(week.sessions)} sessions, limit is {availability.days_per_week} days",
                week,
            )
        runs = sum(session.sport is Sport.RUN for session in week.sessions)
        if runs > availability.swap_rules.max_runs_per_week:
            yield _error(
                "run_frequency",
                f"{runs} runs, limit is {availability.swap_rules.max_runs_per_week}",
                week,
            )
        for session in week.sessions:
            yield from _check_day(session, profile, week)


def _check_day(session: Session, profile: Profile, week: Week) -> Iterator[Issue]:
    """Minutes and kind of session allowed on the session's date. An amended session is
    explicit: the athlete stated the time is there, so the day's minutes do not apply."""
    if session.amendment_id is not None:
        return
    availability = profile.availability
    allowed = availability.minutes(session.date, session.sport)
    exception = availability.exception_on(session.date)
    if exception is None:
        if session.duration_s > allowed * 60:
            day = WEEKDAYS[session.date.weekday()]
            yield _error(
                "day_limit",
                f"{session.date} {session.name}: {_minutes(session.duration_s)} of "
                f"{session.sport} but {day} allows {allowed} min",
                week,
            )
        return
    if session.duration_s > allowed * 60:
        what = (
            f"no {session.sport} possible"
            if allowed == 0
            else f"{_minutes(session.duration_s)} of {session.sport}, {allowed} min allowed"
        )
        yield _error(
            "session_on_unavailable_date",
            f"{session.date} {session.name}: {what} (exception {exception.label()})",
            week,
        )
    forced = exception.bike_activity
    if session.sport is Sport.BIKE and forced is not None:
        activity = ActivityType.MTB if forced is BikeActivity.MTB else ActivityType.RIDE
        if (session.activity_type, session.indoor) != (activity, forced is BikeActivity.TRAINER):
            yield _error(
                "bike_activity_mismatch",
                f"{session.date} {session.name}: {session.activity_type.value}"
                f"{' indoors' if session.indoor else ''}, the exception requires {forced}",
                week,
            )


def _check_progression(plan: Plan, profile: Profile, impact: float) -> Iterator[Issue]:
    loads: list[float] = []
    runs: list[float] = []
    longest: dict[Sport, list[float]] = {sport: [] for sport in Sport}
    for week in plan.weeks:
        load = week_weighted_load(week, impact)
        cap = load_cap(loads, profile)
        if load > cap + LOAD_TOLERANCE_TSS:
            yield _error(
                "ramp_rate",
                f"impact-weighted load {load:.0f} exceeds the ramp cap {cap:.0f} "
                f"(+{profile.preferences.limits.max_weekly_ramp:.0%} over the last 4 weeks' peak)",
                week,
            )
        run_s = week_run_s(week)
        run_cap = run_volume_cap(runs, profile)
        if run_s > run_cap + TIME_TOLERANCE_S:
            yield _error(
                "run_volume_ramp",
                f"run volume {_minutes(run_s)} exceeds the cap {_minutes(run_cap)}",
                week,
            )
        for sport in Sport:
            longest_s = week_longest_s(week, sport)
            long_cap = longest_session_cap(longest[sport], sport, profile)
            if longest_s > long_cap + TIME_TOLERANCE_S:
                yield _error(
                    "long_session_ramp",
                    f"longest {sport} session {_minutes(longest_s)} exceeds the cap "
                    f"{_minutes(long_cap)}",
                    week,
                )
            longest[sport].append(longest_s)
        loads.append(load)
        runs.append(run_s)


def _check_return_from_break(plan: Plan, profile: Profile, impact: float) -> Iterator[Issue]:
    """After a break (calendar exceptions), the first weeks back are capped (rules.py)."""
    if not plan.weeks or not profile.availability.exceptions:
        return
    loads: list[float] = []
    for week in plan.weeks:
        load = week_weighted_load(week, impact)
        back = break_return(profile.availability, week.start, plan.weeks[0].start)
        if back is not None:
            cap = back.cap * reference(loads, baseline_load(profile))
            if load > cap + LOAD_TOLERANCE_TSS:
                yield _error(
                    "return_from_break",
                    f"week {back.week} back after {back.gap_days} days without training: load "
                    f"{load:.0f} is above {back.cap:.0%} of the reference ({cap:.0f})",
                    week,
                )
        loads.append(load)


def _check_recovery_and_taper(plan: Plan, profile: Profile, impact: float) -> Iterator[Issue]:
    max_streak = int(profile.preferences.mesocycle_pattern[0]) + 1
    streak = 0
    loads: list[float] = []
    for previous, week in pairwise([None, *plan.weeks]):
        if week is None:
            continue
        loading = not week.recovery and week.phase not in (Phase.TAPER, Phase.TRANSITION)
        streak = streak + 1 if loading else 0
        if streak == max_streak + 1:
            yield _error(
                "missing_recovery_week",
                f"{streak} loading weeks in a row (max {max_streak} for pattern "
                f"{profile.preferences.mesocycle_pattern})",
                week,
            )
        load = week_weighted_load(week, impact)
        if week.recovery and previous is not None:
            before = week_weighted_load(previous, impact)
            what = "the previous week"
            back = break_return(profile.availability, week.start, plan.weeks[0].start)
            if back is not None and back.week == 1:
                # First week back after a break: compare with the weeks before the break.
                before = reference(loads, baseline_load(profile))
                what = "the weeks before the break"
            if load > RECOVERY_MAX_RATIO * before + LOAD_TOLERANCE_TSS:
                yield _error(
                    "recovery_week_too_hard",
                    f"recovery week load {load:.0f} is more than {RECOVERY_MAX_RATIO:.0%} of "
                    f"{what} ({before:.0f})",
                    week,
                )
        loads.append(load)

    if not plan.weeks:
        return
    first = plan.weeks[0].start
    last = plan.weeks[-1].start + dt.timedelta(days=6)
    in_plan = [goal for goal in plan.goals if first <= goal.date <= last]
    if not in_plan:
        yield _error("no_goal", "no goal falls inside the plan")
        return
    top = min(goal.priority for goal in in_plan)
    for goal in in_plan:
        if goal.priority != top:
            continue
        index = (goal.date - first).days // 7
        week = plan.weeks[index]
        if week.phase is not Phase.TAPER:
            yield _error("missing_taper", f"no taper before '{goal.name}' ({goal.date})", week)
            continue
        before_s = max((w.duration_s for w in plan.weeks[max(0, index - 4) : index]), default=0)
        if before_s and week.duration_s > TAPER_MAX_RATIO * before_s:
            yield _error(
                "taper_volume",
                f"race week of '{goal.name}' has {week.duration_s / 3600:.1f} h, more than "
                f"{TAPER_MAX_RATIO:.0%} of the previous peak week ({before_s / 3600:.1f} h)",
                week,
            )


def _check_hard_sessions(plan: Plan, profile: Profile) -> Iterator[Issue]:
    max_hard = profile.preferences.max_hard_sessions_per_week
    strict = profile.availability.swap_rules.no_hard_run_after_hard_bike
    week_of: dict[dt.date, Week] = {}
    hard: list[Session] = []
    impact_runs: list[Session] = []
    for week in plan.weeks:
        week_hard = [session for session in week.sessions if session.intent.hard]
        if len(week_hard) > max_hard:
            yield _error(
                "too_many_hard_sessions", f"{len(week_hard)} hard sessions, max {max_hard}", week
            )
        elif len(week_hard) > 2 and week.phase is not Phase.PEAK:
            yield _error(
                "too_many_hard_sessions", "3 hard sessions are only allowed in peak weeks", week
            )
        for session in sorted(week.sessions, key=lambda s: s.date):
            week_of[session.date] = week
            if session.intent.hard:
                hard.append(session)
            if session.sport is Sport.RUN and (session.intent.hard or session.slot in LONG_SLOTS):
                impact_runs.append(session)
    for a, b in pairwise(hard):
        if (b.date - a.date).days >= HARD_MIN_GAP_DAYS:
            continue
        message = (
            f"hard sessions on {a.date} ({a.sport}) and {b.date} ({b.sport}) "
            "are less than 48 h apart"
        )
        if a.sport is b.sport or strict:
            yield _error("hard_session_spacing", message, week_of[b.date])
        else:
            yield _warning("hard_session_spacing", message, week_of[b.date])
    for a, b in pairwise(impact_runs):
        back_to_back = a.slot in LONG_SLOTS and b.slot in LONG_SLOTS
        if (b.date - a.date).days <= 1 and not back_to_back:
            yield _error(
                "consecutive_high_impact_runs",
                f"hard or long runs on consecutive days ({a.date}, {b.date})",
                week_of[b.date],
            )


def _check_intensity(plan: Plan, profile: Profile, zones: ZoneSet) -> Iterator[Issue]:
    target = profile.preferences.intensity_ratio.high
    # Ultra plans are low-intensity by design: no "too little intensity" warning for them.
    ultra_goals = {goal.name for goal in plan.goals if goal.type is GoalType.ULTRA_ENDURANCE}
    block_low = block_high = 0
    block: list[Week] = []
    for week in plan.weeks:
        splits = [intensity_split(session, zones) for session in week.sessions]
        low = sum(split[0] for split in splits)
        high = sum(split[1] for split in splits)
        if low + high:
            share = high / (low + high)
            if share > target + INTENSITY_TOLERANCE_WEEK + 1e-9:
                yield _error(
                    "intensity_distribution",
                    f"{share:.0%} of time at moderate/high intensity, target {target:.0%} "
                    f"(+{INTENSITY_TOLERANCE_WEEK:.0%} tolerance)",
                    week,
                )
            elif (
                week.phase in (Phase.BUILD, Phase.PEAK)
                and not week.recovery
                and share < MIN_HIGH_SHARE
                and week.goal not in ultra_goals
            ):
                yield _warning(
                    "low_intensity_share",
                    f"only {share:.0%} of time at moderate/high intensity in a {week.phase} week",
                    week,
                )
        if week.phase in (Phase.TAPER, Phase.TRANSITION):
            continue
        block.append(week)
        block_low += low
        block_high += high
        if week.recovery or week is plan.weeks[-1]:
            total = block_low + block_high
            if len(block) > 1 and total:
                share = block_high / total
                if share > target + INTENSITY_TOLERANCE_MESOCYCLE + 1e-9:
                    yield _error(
                        "intensity_distribution_mesocycle",
                        f"weeks {block[0].index}-{block[-1].index}: {share:.0%} of time at "
                        f"moderate/high intensity, target {target:.0%} "
                        f"(+{INTENSITY_TOLERANCE_MESOCYCLE:.0%} tolerance)",
                        week,
                    )
            block, block_low, block_high = [], 0, 0


def _check_ladders(plan: Plan) -> Iterator[Issue]:
    """A key session is at most one rung above the last completed one of its template
    (a skipped or shortened key session does not count, so the next one repeats the rung)."""
    done: dict[str, int] = {}
    for week in plan.weeks:
        loading = not week.recovery and week.phase not in (Phase.TAPER, Phase.TRANSITION)
        counted: list[tuple[str, int]] = []
        for session in week.sessions:
            rung = progress_rung(session)
            if session.template_id is None or rung is None:
                continue
            if rung > done.get(session.template_id, 0):
                yield _error(
                    "ladder_progression",
                    f"{session.date} {session.name}: rung {rung + 1} of '{session.template_id}' "
                    f"but only {done.get(session.template_id, 0)} completed before",
                    week,
                )
            counted.append((session.template_id, rung))
        if loading:
            for template_id, rung in counted:
                done[template_id] = max(done.get(template_id, 0), rung + 1)


def _check_amendments(plan: Plan) -> Iterator[Issue]:
    """Amended sessions match the amendment log: each active session amendment has its
    pinned session (none for a skip), and nothing else is planned on a skipped date."""
    week_of = {s.date: week for week in plan.weeks for s in week.sessions}
    sessions = [s for week in plan.weeks for s in week.sessions]
    active = {a.id: a for a in plan.active_amendments(AmendmentKind.SESSION)}
    for amendment in active.values():
        results = [s for s in sessions if s.amendment_id == amendment.id]
        if amendment.skip:
            others = [s for s in sessions if s.date == amendment.date and s.amendment_id is None]
            for session in results + others:
                yield _error(
                    "amendment_mismatch",
                    f"{session.date} {session.name}: planned on a day skipped by "
                    f"amendment {amendment.id}",
                    week_of[session.date],
                )
        elif len(results) != 1:
            yield _error(
                "amendment_mismatch",
                f"amendment {amendment.id} ({amendment.date}) has {len(results)} session(s) "
                "in the plan, expected 1",
            )
    for session in sessions:
        if session.amendment_id is not None and (
            session.amendment_id not in active or not session.pinned
        ):
            yield _error(
                "amendment_mismatch",
                f"{session.date} {session.name}: amendment {session.amendment_id} is not an "
                "active amendment of this plan, or the session is not pinned",
                week_of[session.date],
            )


def _check_strength(plan: Plan, profile: Profile) -> Iterator[Issue]:
    """Strength sessions (F10): placement, phase and prescription, equipment, race
    proximity and weekly count."""
    endurance = {s.date: s for week in plan.weeks for s in week.sessions}
    settings = profile.availability.strength
    injury = bool(profile.athlete.injury_notes.strip())
    previous: StrengthSession | None = None
    for week in plan.weeks:
        days = len(training_days(week, explicit=False))
        if not settings.allow_extra_days and days > profile.availability.days_per_week:
            yield _error(
                "strength_placement",
                f"{days} training days with strength, limit is "
                f"{profile.availability.days_per_week} (availability.strength.allow_extra_days "
                "is false)",
                week,
            )
        allowed = weekly_count(week.phase, settings)
        if len(week.strength) > allowed:
            yield _error(
                "strength_count",
                f"{len(week.strength)} strength sessions, {week.phase} weeks allow {allowed} "
                f"(phase table capped by availability.strength.sessions_per_week)",
                week,
            )
        for session in sorted(week.strength, key=lambda s: s.date):
            label = f"{session.date} {session.name}"
            explicit = session.amendment_id is not None
            for problem in day_problems(session.date, endurance, profile, plan.goals, explicit):
                code, why = DAY_PROBLEMS[problem]
                yield _error(code, f"{label}: {why}", week)
            if previous is not None and (session.date - previous.date).days < (
                STRENGTH_MIN_GAP_DAYS
            ):
                yield _error(
                    "strength_placement",
                    f"strength sessions on {previous.date} and {session.date} are less than "
                    "48 h apart",
                    week,
                )
            previous = session
            yield from _check_strength_phase(session, week)
            available = profile.availability.equipment(session.date)
            available |= set(_amended_equipment(plan, session))
            for exercise in session.exercises:
                spec = EXERCISES.get(exercise.id)
                if spec is None:
                    yield _error(
                        "strength_equipment", f"{label}: unknown exercise '{exercise.id}'", week
                    )
                    continue
                if exercise_equipment(spec, available) is None:
                    needs = " or ".join(item.value for item in spec.equipment)
                    yield _error(
                        "strength_equipment",
                        f"{label}: {spec.name} needs {needs}, not available that day",
                        week,
                    )
                if injury and spec.high_impact:
                    yield _error(
                        "strength_equipment",
                        f"{label}: {spec.name} is a higher-impact exercise, left out while "
                        "athlete.injury_notes is set",
                        week,
                    )


def _amended_equipment(plan: Plan, session: StrengthSession) -> list[Equipment]:
    """Equipment stated by the amendment that produced the session (none: the profile's)."""
    return []


def _check_strength_phase(session: StrengthSession, week: Week) -> Iterator[Issue]:
    label = f"{session.date} {session.name}"
    expected = PHASE_TEMPLATE[week.phase]
    template = STRENGTH_TEMPLATES.get(session.template_id)
    if session.template_id != expected or session.phase is not week.phase or template is None:
        yield _error(
            "strength_phase",
            f"{label}: template '{session.template_id}' in a {week.phase} week "
            f"(expected '{expected}')",
            week,
        )
        return
    rungs = [session.rung] if session.rung is not None else range(len(template.ladder))
    if any(rung >= len(template.ladder) for rung in rungs):
        yield _error("strength_phase", f"{label}: rung {session.rung} is off the ladder", week)
        return
    main = [e for e in session.exercises if not e.accessory and e.unit == "reps"]

    def matches(rung: int) -> bool:
        sets, reps, rir = prescription(template.id, rung, week.recovery)
        return all((e.reps, e.rir) == (reps, rir) and e.sets <= sets for e in main)

    fits = any(matches(rung) for rung in rungs)
    if not fits:
        yield _error(
            "strength_phase",
            f"{label}: the prescription does not match the {template.name.lower()} ladder"
            f"{' (recovery week)' if week.recovery else ''}",
            week,
        )


def _check_targets(plan: Plan) -> Iterator[Issue]:
    for week in plan.weeks:
        for session in week.sessions:
            for step in session.flat_steps():
                resolved = step.resolved
                ok = resolved is not None and (
                    step.target.kind is TargetKind.RPE
                    or (step.target.kind is TargetKind.POWER and resolved.power_w is not None)
                    or (step.target.kind is TargetKind.PACE and resolved.pace_s_per_km is not None)
                    or (step.target.kind is TargetKind.HR and resolved.hr_bpm is not None)
                )
                if not ok:
                    yield _error(
                        "unresolved_target",
                        f"{session.date} {session.name}: a {step.target.kind} target has no "
                        "absolute value (missing threshold?)",
                        week,
                    )
                    break


def validate_plan(plan: Plan, profile: Profile, today: dt.date | None = None) -> list[Issue]:
    """All rule violations of a plan. `today` defaults to the plan's creation date.

    The plan's unavailable amendments are added to the profile's calendar exceptions.
    """
    profile = plan.effective_profile(profile)
    zones = zone_set(profile.thresholds.zone_models)
    impact = run_impact_multiplier(profile)
    return [
        *_check_structure(plan, today or plan.created),
        *_check_availability(plan, profile),
        *_check_progression(plan, profile, impact),
        *_check_recovery_and_taper(plan, profile, impact),
        *_check_return_from_break(plan, profile, impact),
        *_check_hard_sessions(plan, profile),
        *_check_intensity(plan, profile, zones),
        *_check_ladders(plan),
        *_check_amendments(plan),
        *_check_strength(plan, profile),
        *_check_targets(plan),
    ]


def assert_valid(plan: Plan, profile: Profile, today: dt.date | None = None) -> list[Issue]:
    """Raise PlanValidationError if the plan has errors; return its warnings otherwise."""
    issues = validate_plan(plan, profile, today)
    errors = [issue for issue in issues if issue.severity is Severity.ERROR]
    if errors:
        raise PlanValidationError(errors)
    return issues
