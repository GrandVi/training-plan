"""Weekly scheduler.

For each week of the skeleton:
1. place sport-agnostic slots (long, key, easy) on days,
2. give each slot a sport (goal sport first, the other sport as aerobic support),
3. size the sessions to the week's volume target,
4. degrade the week (less volume, lower ladder levels, fewer keys) until it is
   under the load-ramp cap and the intensity-distribution limit.

Availability, run frequency, run-volume and long-session caps are respected by
construction; validation.validate_plan re-checks everything afterwards.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from itertools import combinations, pairwise

from endurance_planner.load import (
    intensity_split,
    run_impact_multiplier,
    week_weighted_load,
    weighted_load,
)
from endurance_planner.models.enums import (
    ActivityType,
    Experience,
    GoalType,
    Intent,
    Phase,
    Priority,
    Severity,
    Slot,
    Sport,
    Weekday,
)
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Repeat, Session, Step, Week
from endurance_planner.models.profile import Goal, Profile
from endurance_planner.periodization import WeekSkeleton
from endurance_planner.rules import (
    HARD_MIN_GAP_DAYS,
    INTENSITY_TOLERANCE_MESOCYCLE,
    LOAD_STEP_TSS,
    LONG_START_S,
    RAMP_WINDOW,
    RUN_BOOTSTRAP_S,
    STARTER_VOLUME_S,
    baseline_load,
    current_run_s,
    load_cap,
    longest_recent_s,
    longest_session_cap,
    reference,
    run_volume_cap,
    week_longest_s,
    week_run_s,
)
from endurance_planner.specificity import GOAL_DISTANCE_KM, GOAL_SPECS, GoalSpec
from endurance_planner.swap import convert_session, other_sport
from endurance_planner.targets import express_blocks, resolve_session
from endurance_planner.templates import (
    OPENERS,
    TEMPLATES,
    Template,
    key_blocks,
    long_blocks,
    rung_label,
    steady_blocks,
)
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.zones import zone_set

WEEKDAYS = list(Weekday)
KEY_SLOTS = (Slot.KEY_1, Slot.KEY_2, Slot.KEY_3)
LONG_SLOTS = (Slot.LONG, Slot.LONG_2)

# --- tunable scheduling rules -------------------------------------------------
# Planned weekly ramp of volume and load (always below limits.max_weekly_ramp).
PLANNED_RAMP = {
    Experience.BEGINNER: 0.04,
    Experience.INTERMEDIATE: 0.06,
    Experience.ADVANCED: 0.07,
}
EASY_MIN_S = {Sport.BIKE: 30 * 60, Sport.RUN: 20 * 60}
EASY_MAX_S = {Sport.BIKE: 4 * 3600, Sport.RUN: 75 * 60}
LONG_MIN_S = {Sport.BIKE: 60 * 60, Sport.RUN: 40 * 60}
KEY_MIN_S = {Sport.BIKE: 45 * 60, Sport.RUN: 35 * 60}  # time needed to host a key session
LONG_KEEP = 0.75  # share of the long run protected when key runs compete for run volume
# Easy slots of a run plan become runs only while a decent easy run fits in the run volume
# left after realistic key and long runs; otherwise they are bike sessions.
EASY_RUN_WORTH_S = 30 * 60
RUN_KEY_TYPICAL_S = 50 * 60
EASY_RUN_TYPICAL_S = 40 * 60
PRE_RACE_MAX_S = 40 * 60  # day before a race: short and easy
LONG_2_FACTOR = 0.6  # second day of a back-to-back, relative to the long session
RECOVERY_LONG_FACTOR = 0.75  # long session and run volume in recovery weeks
TRANSITION_VOLUME = 0.5  # volume of the week after a main race
RESTART_VOLUME = 0.85  # volume level when training resumes after a main race
MINOR_RACE_VOLUME = {Priority.A: 0.8, Priority.B: 0.8, Priority.C: 1.0}  # mini-taper
SECONDARY_RUN_SHARE = 0.30  # max share of weekly time for running in a bike-goal plan
TECHNICAL_MIN_S = 45 * 60
# Assignment priority of slots to the goal sport when run volume is limited.
RUN_PRIORITY = {
    Slot.KEY_1: 0,
    Slot.LONG: 1,
    Slot.KEY_2: 2,
    Slot.KEY_3: 3,
    Slot.LONG_2: 4,
    Slot.EASY: 5,
}

PHASE_WHY = {
    Phase.BASE: "Base phase: aerobic foundation first, intensity stays limited.",
    Phase.BUILD: "Build phase: key sessions progress one step at a time.",
    Phase.PEAK: "Peak phase: race-specific work while volume holds steady.",
    Phase.TAPER: "Taper: volume drops, short intensity keeps you sharp.",
    Phase.TRANSITION: "Transition week: easy training to recover before the next block.",
}
RECOVERY_WHY = "Recovery week: reduced dose to absorb the previous block."
EASY_WHAT = "Low-intensity volume below LT1 builds the aerobic base and keeps the week near 80/20."
LONG_WHAT = "Long steady session for durability and fat oxidation; it grows by at most 10% a week."
LONG_2_WHAT = "Second long day on tired legs: ultra-specific durability at low mechanical cost."
PRE_RACE_WHAT = "Short easy session the day before the race to stay loose."
TECHNICAL_WHAT = "Endurance ride with a skills focus (cornering, descending, line choice)."
SUBSTITUTE_WHY = {
    Sport.BIKE: "Done on the bike to keep run volume and impact within their caps.",
    Sport.RUN: "Done as a run because bike time is not available that day.",
}


@dataclass
class _Placed:
    slot: Slot
    date: dt.date
    sport: Sport | None = None


@dataclass
class _Caps:
    load: float  # impact-weighted load allowed this week
    run_s: float  # run volume allowed this week
    longest_s: dict[Sport, float]  # longest session allowed per sport
    long_target_s: dict[Sport, float]  # planned long-session duration per sport


@dataclass
class _Knobs:
    hours_s: float
    levels: dict[Slot, int | None]  # ladder level per key slot; None = key dropped
    quality_long: bool


def _floor5(seconds: float) -> int:
    return int(seconds // 300) * 300


class Scheduler:
    """Builds weeks one after the other, carrying progression state between them."""

    def __init__(self, profile: Profile, thresholds: ResolvedThresholds) -> None:
        self.profile = profile
        self.thresholds = thresholds
        self.zones = zone_set(profile.thresholds.zone_models)
        self.impact = run_impact_multiplier(profile)
        limits = profile.preferences.limits
        self.ramp = min(PLANNED_RAMP[profile.athlete.experience], 0.9 * limits.max_weekly_ramp)
        self.issues: list[Issue] = []
        self.uses: dict[str, int] = {}  # template id -> next ladder level
        self.rotation: dict[tuple[str, Phase], int] = {}  # (goal, phase) -> loading weeks done
        self.level_s: float | None = None  # current weekly volume level
        self._goal_pace: dict[str, float | None] = {}
        self.enabled = {
            sport
            for sport in Sport
            if profile.thresholds.anchored(sport)
            and any(day.of(sport) for day in profile.availability.weekdays.values())
        }

    # ------------------------------------------------------------------ helpers
    def _warn(self, code: str, message: str, week: int | None = None) -> None:
        self.issues.append(Issue(severity=Severity.WARNING, code=code, message=message, week=week))

    def _minutes(self, date: dt.date, sport: Sport) -> int:
        return self.profile.availability.weekdays[WEEKDAYS[date.weekday()]].of(sport)

    def _day_minutes(self, date: dt.date) -> int:
        return max((self._minutes(date, sport) for sport in self.enabled), default=0)

    def _mode(self, sport: Sport) -> str:
        mode = self.profile.thresholds.target_mode
        return mode.bike if sport is Sport.BIKE else mode.run

    def _why(self, sk: WeekSkeleton) -> str:
        return RECOVERY_WHY if sk.recovery else PHASE_WHY[sk.phase]

    def goal_pace(self, goal: Goal) -> float | None:
        """Goal race pace as a fraction of threshold speed (run goals only)."""
        if goal.name not in self._goal_pace:
            spec = GOAL_SPECS[goal.type]
            pace = spec.race_pace
            distance = GOAL_DISTANCE_KM.get(goal.type)
            threshold = self.thresholds.threshold_pace_s
            if pace and distance and goal.target.time and threshold:
                wanted = threshold / (goal.target.time / distance)
                pace = min(max(wanted, spec.race_pace_range[0]), spec.race_pace_range[1])
                if pace != wanted:
                    self._warn(
                        "goal_pace_clamped",
                        f"'{goal.name}': target time implies {wanted:.0%} of threshold speed, "
                        "outside the usual range for this distance; race-pace work uses "
                        f"{pace:.0%}",
                    )
            self._goal_pace[goal.name] = pace
        return self._goal_pace[goal.name]

    # ------------------------------------------------------------------ targets
    def _volume(self, sk: WeekSkeleton, history: list[Week]) -> float:
        fitness = self.profile.current_fitness
        budget = self.profile.availability.weekly_hours_budget * 3600
        if self.level_s is None:
            current = (fitness.weekly_hours.bike + fitness.weekly_hours.run) * 3600
            self.level_s = min(budget, max(current, STARTER_VOLUME_S))
            if fitness.weeks_of_consistent_training < 4:
                self.level_s *= 0.9
                self._warn(
                    "low_consistency",
                    "fewer than 4 weeks of consistent training: the plan starts 10% below "
                    "the reported weekly hours",
                )
        elif sk.phase in (Phase.BASE, Phase.BUILD) and not sk.recovery:
            self.level_s = min(budget, self.level_s * (1 + self.ramp))
        target = self.level_s
        # Reductions are relative to what was actually done in the last weeks.
        done = max((float(week.duration_s) for week in history[-RAMP_WINDOW:]), default=target)
        if sk.recovery or sk.phase in (Phase.TAPER, Phase.TRANSITION):
            target = min(target, done)
        if sk.recovery:
            target *= self.profile.preferences.limits.recovery_week_factor
        elif sk.phase is Phase.TAPER:
            target *= sk.volume_factor
        elif sk.phase is Phase.TRANSITION:
            target *= TRANSITION_VOLUME
            self.level_s *= RESTART_VOLUME
        for race in sk.minor_races:
            target *= MINOR_RACE_VOLUME[race.priority]
        return target

    def _phase_factor(self, sk: WeekSkeleton) -> float | None:
        """Reduction applied to run volume and long sessions; None in loading weeks."""
        if sk.recovery:
            return RECOVERY_LONG_FACTOR
        if sk.phase is Phase.TAPER:
            return sk.volume_factor
        if sk.phase is Phase.TRANSITION:
            return TRANSITION_VOLUME
        return None

    def _caps(
        self,
        sk: WeekSkeleton,
        history: list[Week],
        goal_sport: Sport,
        spec: GoalSpec,
        hours_s: float,
    ) -> _Caps:
        profile = self.profile
        limits = profile.preferences.limits
        factor = self._phase_factor(sk)
        hold = not history or sk.phase is Phase.PEAK

        loads = [week_weighted_load(week, self.impact) for week in history]
        ref = reference(loads, baseline_load(profile))
        if not history:
            load = ref
        elif sk.recovery:
            load = (limits.recovery_week_factor + 0.05) * loads[-1]
        elif factor is not None:
            load = (factor + 0.05) * ref
        else:
            load = max(ref * (1 + self.ramp), ref + LOAD_STEP_TSS)
        load = min(load, load_cap(loads, profile))

        run_history = [float(week_run_s(week)) for week in history]
        run_ref = reference(run_history, current_run_s(profile))
        run_hard = run_volume_cap(run_history, profile)
        if Sport.RUN not in self.enabled:
            run = 0.0
        elif goal_sport is Sport.RUN:
            if factor is not None:
                run = factor * run_ref
            elif hold:
                run = max(run_ref, RUN_BOOTSTRAP_S) if not history else run_ref
            else:
                run = run_hard
        else:
            run = min(current_run_s(profile), SECONDARY_RUN_SHARE * hours_s) * (factor or 1.0)
        run = min(run, run_hard)

        longest: dict[Sport, float] = {}
        long_target: dict[Sport, float] = {}
        for sport in Sport:
            history_s = [float(week_longest_s(week, sport)) for week in history]
            longest[sport] = longest_session_cap(history_s, sport, profile)
            long_ref = reference(history_s, longest_recent_s(profile, sport)) or LONG_START_S[sport]
            if factor is not None:
                target = factor * long_ref
            elif hold:
                target = long_ref
            else:
                target = longest[sport]
            long_target[sport] = min(target, longest[sport], self._long_max_s(sk.goal, spec, sport))
        return _Caps(load=load, run_s=run, longest_s=longest, long_target_s=long_target)

    def _long_max_s(self, goal: Goal, spec: GoalSpec, sport: Sport) -> float:
        ceiling = spec.long_max_min[sport] * 60.0
        if goal.target.duration_h and sport is self.profile.goal_sport(goal):
            # No need to train longer than about 80% of the expected race duration.
            ceiling = min(ceiling, max(0.8 * goal.target.duration_h * 3600, LONG_MIN_S[sport] * 2))
        return ceiling

    # ------------------------------------------------------------------ templates
    def _unlocked(self, template: Template) -> bool:
        return not template.prerequisites or any(self.uses.get(t) for t in template.prerequisites)

    def _key_templates(self, sk: WeekSkeleton, spec: GoalSpec, goal_sport: Sport) -> list[Template]:
        if sk.phase is Phase.TRANSITION:
            return []
        if sk.race is not None:
            return [TEMPLATES[OPENERS[goal_sport]]]
        phase = Phase.PEAK if sk.phase is Phase.TAPER else sk.phase
        rotations = list(spec.keys[goal_sport].get(phase, ()))
        max_hard = self.profile.preferences.max_hard_sessions_per_week
        limit = min(max_hard, 2)
        if sk.recovery or sk.phase is Phase.TAPER:
            limit = 1
        elif sk.phase is Phase.PEAK and max_hard >= 3 and rotations:
            rotations.append(rotations[0])  # peak-block option: a third key session
            limit = 3
        limit -= len(sk.minor_races)
        count = self.rotation.get((sk.goal.name, phase), 0)
        picked: list[Template] = []
        for rotation in rotations:
            if len(picked) >= limit:
                break
            shift = count % len(rotation)
            for template_id in rotation[shift:] + rotation[:shift]:
                template = TEMPLATES[template_id]
                if self._unlocked(template) and template not in picked:
                    picked.append(template)
                    break
        return picked

    def _level(self, sk: WeekSkeleton, template: Template) -> int:
        done = self.uses.get(template.id, 0)
        if sk.recovery or sk.phase in (Phase.TAPER, Phase.TRANSITION):
            return max(0, done - 2)  # one rung below the last one completed
        return min(done, len(template.ladder) - 1)

    # ------------------------------------------------------------------ placement
    def _place(
        self, sk: WeekSkeleton, history: list[Week], spec: GoalSpec, key_count: int
    ) -> tuple[list[_Placed], set[dt.date]]:
        availability = self.profile.availability
        races = {goal.date for goal in sk.minor_races}
        if sk.race is not None:
            races.add(sk.race.date)
        eves = {date - dt.timedelta(days=1) for date in races}
        dates = [sk.start + dt.timedelta(days=offset) for offset in range(7)]
        available = [
            date
            for date in dates
            if self._day_minutes(date) > 0
            and date not in races
            and not (sk.race is not None and date > sk.race.date)
        ]
        day_count = min(availability.days_per_week, len(available))
        placed: list[_Placed] = []

        long_date = sk.start + dt.timedelta(days=WEEKDAYS.index(availability.long_session_day))
        if day_count and sk.race is None and long_date in available and long_date not in eves:
            placed.append(_Placed(Slot.LONG, long_date))
            back_to_back = spec.back_to_back and sk.phase in (Phase.BUILD, Phase.PEAK)
            if back_to_back and not sk.recovery and day_count >= 2:
                for delta in (1, -1):
                    second = long_date + dt.timedelta(days=delta)
                    if second in available and second not in eves:
                        placed.append(_Placed(Slot.LONG_2, second))
                        break
        long_dates = {p.date for p in placed}

        free = [date for date in available if date not in long_dates and date not in eves]
        if sk.race is not None:
            free = [date for date in free if 2 <= (sk.race.date - date).days <= 4]
        previous_hard = [s.date for s in history[-1].sessions if s.intent.hard] if history else []
        best: tuple[dt.date, ...] = ()
        for count in range(min(key_count, day_count - len(placed)), 0, -1):
            best_score: float | None = None
            for combo in combinations(free, count):
                if any((b - a).days < HARD_MIN_GAP_DAYS for a, b in pairwise(combo)):
                    continue
                if any(
                    (date - hard).days < HARD_MIN_GAP_DAYS
                    for date in combo
                    for hard in previous_hard
                ):
                    continue
                score = self._placement_score(combo, long_dates)
                if best_score is None or score > best_score:
                    best, best_score = combo, score
            if best:
                break
        placed.extend(_Placed(KEY_SLOTS[i], date) for i, date in enumerate(best))

        used = long_dates | set(best)
        rest = sorted(
            (date for date in available if date not in used),
            key=lambda date: (-self._day_minutes(date), date),
        )
        placed.extend(_Placed(Slot.EASY, date) for date in rest[: day_count - len(placed)])
        return sorted(placed, key=lambda p: p.date), eves

    def _placement_score(self, combo: tuple[dt.date, ...], long_dates: set[dt.date]) -> float:
        indoor = self.profile.availability.indoor_trainer_days
        day = dt.timedelta(days=1)
        score = 0.0
        for date in combo:
            score += 3.0 if WEEKDAYS[date.weekday()] in indoor else 0.0
            score -= 4.0 if date - day in long_dates else 0.0  # tired legs after the long day
            score -= 1.0 if date + day in long_dates else 0.0
            score += self._day_minutes(date) / 600
        if len(combo) > 1:
            score += 0.5 * min((b - a).days for a, b in pairwise(combo))
        return score

    def _assign_sports(
        self,
        placed: list[_Placed],
        history: list[Week],
        goal_sport: Sport,
        caps: _Caps,
        race_week: bool,
        eves: set[dt.date],
    ) -> None:
        rules = self.profile.availability.swap_rules
        runs = 0
        run_left = caps.run_s
        long_dates = {p.date for p in placed if p.slot in LONG_SLOTS}
        impact_dates = set()
        if history:
            impact_dates = {
                s.date
                for s in history[-1].sessions
                if s.sport is Sport.RUN and (s.intent.hard or s.slot in LONG_SLOTS)
            }

        def need(p: _Placed, sport: Sport) -> int:
            """Shortest useful duration of this slot."""
            if p.slot in KEY_SLOTS:
                return KEY_MIN_S[sport]
            return LONG_MIN_S[sport] if p.slot in LONG_SLOTS else EASY_MIN_S[sport]

        def reserve(p: _Placed) -> float:
            """Run volume set aside for this slot, so that later slots see what is left."""
            if p.slot in KEY_SLOTS:
                return KEY_MIN_S[Sport.RUN]
            if p.slot in LONG_SLOTS:
                return LONG_KEEP * caps.long_target_s[Sport.RUN]
            return EASY_MIN_S[Sport.RUN]

        def can(p: _Placed, sport: Sport) -> bool:
            if sport not in self.enabled or self._minutes(p.date, sport) * 60 < need(p, sport):
                return False
            if sport is Sport.BIKE:
                return True
            if runs >= rules.max_runs_per_week or run_left < need(p, sport):
                return False
            if p.slot is Slot.EASY:
                return True
            # No hard or long run next to another one (a back-to-back long pair is allowed).
            others = impact_dates - (long_dates if p.slot in LONG_SLOTS else set())
            return not any(abs((p.date - date).days) == 1 for date in others)

        def take(p: _Placed, sport: Sport) -> None:
            nonlocal runs, run_left
            p.sport = sport
            if sport is Sport.RUN:
                runs += 1
                run_left -= min(reserve(p), run_left)
                if p.slot is not Slot.EASY:
                    impact_dates.add(p.date)

        if goal_sport is Sport.RUN:
            # Key and long sessions are runs while run volume allows. Easy sessions go to
            # the athlete's primary sport (bike volume supports a running plan); in race
            # week everything stays in the goal sport.
            primary = Sport.RUN if race_week else self.profile.preferences.primary_sport
            typical_left = caps.run_s  # run volume left after typical session durations
            # The pre-race shakeout comes first among easy sessions.
            order = sorted(placed, key=lambda p: (RUN_PRIORITY[p.slot], p.date not in eves, p.date))
            for p in order:
                easy_slot = p.slot is Slot.EASY
                first = Sport.RUN
                if easy_slot and (primary is Sport.BIKE or typical_left < EASY_RUN_WORTH_S):
                    first = Sport.BIKE
                for sport in (first, other_sport(first)):
                    allowed = sport is Sport.RUN or easy_slot or rules.allow_bike_instead_of_run
                    if allowed and can(p, sport):
                        take(p, sport)
                        if sport is Sport.RUN:
                            if p.slot in KEY_SLOTS:
                                typical_left -= RUN_KEY_TYPICAL_S
                            elif p.slot in LONG_SLOTS:
                                typical_left -= caps.long_target_s[Sport.RUN]
                            elif Sport.BIKE in self.enabled:
                                # Few, longer easy runs; the other easy slots go to the bike.
                                typical_left -= EASY_MAX_S[Sport.RUN]
                            else:
                                typical_left -= EASY_RUN_TYPICAL_S
                        break
            return
        easy = [p for p in placed if p.slot is Slot.EASY]
        for p in placed:
            if p.slot is Slot.EASY:
                continue
            if can(p, Sport.BIKE):
                take(p, Sport.BIKE)
            elif rules.allow_run_instead_of_bike and can(p, Sport.RUN):
                take(p, Sport.RUN)  # time-limited day
        wanted_runs = math.ceil(run_left / 3600) if run_left >= EASY_MIN_S[Sport.RUN] else 0
        # Runs go to the days with the least bike time first.
        for p in sorted(easy, key=lambda p: (self._minutes(p.date, Sport.BIKE), p.date)):
            if wanted_runs > 0 and can(p, Sport.RUN):
                take(p, Sport.RUN)
                wanted_runs -= 1
            elif can(p, Sport.BIKE):
                take(p, Sport.BIKE)
            elif can(p, Sport.RUN):
                take(p, Sport.RUN)

    # ------------------------------------------------------------------ sessions
    def _session(
        self,
        sk: WeekSkeleton,
        p: _Placed,
        sport: Sport,
        blocks: list[Step | Repeat],
        *,
        intent: Intent,
        name: str,
        what: str,
        template: Template | None = None,
        technical: bool = False,
        notes: str = "",
    ) -> Session:
        indoor_day = WEEKDAYS[p.date.weekday()] in self.profile.availability.indoor_trainer_days
        indoor = sport is Sport.BIKE and indoor_day and p.slot not in LONG_SLOTS
        mtb_goal = sk.goal.type in (GoalType.MTB_XC, GoalType.MTB_MARATHON)
        off_road = technical or p.slot in LONG_SLOTS or (template is not None and template.mtb)
        activity = ActivityType.RUN
        if sport is Sport.BIKE:
            activity = (
                ActivityType.MTB if mtb_goal and off_road and not indoor else ActivityType.RIDE
            )
        return Session(
            date=p.date,
            sport=sport,
            activity_type=activity,
            slot=p.slot,
            intent=intent,
            name=name,
            steps=express_blocks(blocks, sport, self._mode(sport)),
            rationale=f"{what} {self._why(sk)}",
            template_id=template.id if template else None,
            indoor=indoor,
            technical=technical,
            notes=notes,
        )

    def _key_session(
        self, sk: WeekSkeleton, p: _Placed, template: Template, level: int, cap_s: float
    ) -> tuple[Session, int] | None:
        """Key session at the highest level <= `level` that fits in cap_s seconds."""
        assert p.sport is not None
        for lv in range(level, -1, -1):
            for short in (False, True):
                blocks = key_blocks(
                    template,
                    lv,
                    short=short,
                    pace_s=self.thresholds.threshold_pace_s,
                    goal_pace=self.goal_pace(sk.goal),
                )
                session = self._session(
                    sk,
                    p,
                    template.sport,
                    blocks,
                    intent=template.intent,
                    name=f"{template.name} {rung_label(template, lv)}",
                    what=template.rationale,
                    template=template,
                    notes=template.notes,
                )
                if p.sport is not template.sport:
                    session, _ = convert_session(
                        session, self.impact, self._mode(p.sport), self.zones
                    )
                    session = session.model_copy(
                        update={"rationale": f"{session.rationale} {SUBSTITUTE_WHY[p.sport]}"}
                    )
                if session.duration_s <= cap_s:
                    return session, lv
        return None

    def _assemble(
        self,
        sk: WeekSkeleton,
        spec: GoalSpec,
        placed: list[_Placed],
        templates: dict[Slot, Template],
        knobs: _Knobs,
        caps: _Caps,
        eves: set[dt.date],
    ) -> tuple[list[Session], dict[Slot, int]]:
        sessions: list[Session] = []
        used_levels: dict[Slot, int] = {}
        left = knobs.hours_s
        run_left = caps.run_s
        easy: list[_Placed] = []

        def cap(p: _Placed, run_reserve: float = 0.0) -> float:
            assert p.sport is not None
            limit = min(self._minutes(p.date, p.sport) * 60.0, caps.longest_s[p.sport])
            return min(limit, run_left - run_reserve) if p.sport is Sport.RUN else limit

        def add(session: Session) -> None:
            nonlocal left, run_left
            sessions.append(session)
            left -= session.duration_s
            if session.sport is Sport.RUN:
                run_left -= session.duration_s

        long_runs = [p for p in placed if p.slot in LONG_SLOTS and p.sport is Sport.RUN]
        long_reserve = LONG_KEEP * caps.long_target_s[Sport.RUN] if long_runs else 0.0
        bike_allowed = self.profile.availability.swap_rules.allow_bike_instead_of_run
        for p in placed:
            if p.slot not in KEY_SLOTS:
                continue
            level = knobs.levels.get(p.slot)
            built = None
            if level is not None:
                template = templates[p.slot]
                built = self._key_session(sk, p, template, level, cap(p, long_reserve))
                if (
                    built is None
                    and p.sport is Sport.RUN
                    and bike_allowed
                    and Sport.BIKE in self.enabled
                ):
                    # Not enough run volume for a key run: same session on the bike.
                    p = _Placed(p.slot, p.date, Sport.BIKE)
                    built = self._key_session(sk, p, template, level, cap(p))
            if built is None:
                easy.append(_Placed(Slot.EASY, p.date, p.sport))
            else:
                add(built[0])
                used_levels[p.slot] = built[1]

        for p in placed:
            if p.slot not in LONG_SLOTS:
                continue
            assert p.sport is not None
            target = caps.long_target_s[p.sport] * (LONG_2_FACTOR if p.slot is Slot.LONG_2 else 1.0)
            duration = _floor5(min(target, cap(p), left))
            if duration < LONG_MIN_S[p.sport]:
                easy.append(_Placed(Slot.EASY, p.date, p.sport))
                continue
            quality = knobs.quality_long and p.slot is Slot.LONG and sk.phase in spec.quality_long
            quality = quality and not sk.recovery
            second = p.slot is Slot.LONG_2
            add(
                self._session(
                    sk,
                    p,
                    p.sport,
                    long_blocks(p.sport, duration, quality),
                    intent=Intent.ENDURANCE,
                    name=("Long ride" if p.sport is Sport.BIKE else "Long run")
                    + (" (day 2)" if second else ""),
                    what=LONG_2_WHAT if second else LONG_WHAT,
                    notes=spec.long_notes,
                )
            )

        easy.extend(p for p in placed if p.slot is Slot.EASY)
        technical_done = not spec.technical or sk.phase in (Phase.TAPER, Phase.TRANSITION)
        pending = sorted(easy, key=lambda p: (self._minutes(p.date, p.sport or Sport.BIKE), p.date))
        while pending:
            p = pending.pop(0)
            assert p.sport is not None
            pre_race = p.date in eves

            def limit(sport: Sport, p: _Placed = p, pre_race: bool = pre_race) -> float:
                value = min(
                    self._minutes(p.date, sport) * 60.0,
                    caps.longest_s[sport],
                    EASY_MAX_S[sport],
                    left / (len(pending) + 1),
                )
                if sport is Sport.RUN:
                    value = min(value, run_left / (1 + sum(q.sport is Sport.RUN for q in pending)))
                return min(value, PRE_RACE_MAX_S) if pre_race else value

            duration = _floor5(limit(p.sport))
            if (
                duration < EASY_MIN_S[p.sport]
                and p.sport is Sport.RUN
                and Sport.BIKE in self.enabled
            ):
                # Not enough run volume left: do it on the bike instead.
                p = _Placed(Slot.EASY, p.date, Sport.BIKE)
                duration = _floor5(limit(Sport.BIKE))
            assert p.sport is not None
            if duration < EASY_MIN_S[p.sport]:
                continue
            indoor_day = WEEKDAYS[p.date.weekday()] in self.profile.availability.indoor_trainer_days
            technical = (
                not technical_done
                and p.sport is Sport.BIKE
                and not indoor_day
                and not pre_race
                and duration >= TECHNICAL_MIN_S
            )
            technical_done = technical_done or technical
            name = "Endurance ride" if p.sport is Sport.BIKE else "Easy run"
            what = EASY_WHAT
            if pre_race:
                name, what = "Pre-race shakeout", PRE_RACE_WHAT
            elif technical:
                name, what = "Endurance ride + MTB skills", TECHNICAL_WHAT
            add(
                self._session(
                    sk,
                    p,
                    p.sport,
                    steady_blocks(p.sport, duration),
                    intent=Intent.TECHNICAL if technical else Intent.ENDURANCE,
                    name=name,
                    what=what,
                    technical=technical,
                )
            )
        sessions.sort(key=lambda s: s.date)
        return sessions, used_levels

    @staticmethod
    def _lower(knobs: _Knobs) -> bool:
        """Lower the highest key level by one, or drop the last key. False if nothing is left."""
        active = {slot: level for slot, level in knobs.levels.items() if level is not None}
        if not active:
            return False
        slot = max(active, key=lambda s: (active[s], s.value))
        if active[slot] > 0:
            knobs.levels[slot] = active[slot] - 1
        else:
            knobs.levels[max(active, key=lambda s: s.value)] = None
        return True

    def _fit(
        self,
        sk: WeekSkeleton,
        spec: GoalSpec,
        placed: list[_Placed],
        templates: dict[Slot, Template],
        hours_s: float,
        caps: _Caps,
        eves: set[dt.date],
    ) -> tuple[list[Session], dict[Slot, int]]:
        knobs = _Knobs(
            hours_s=hours_s,
            levels={slot: self._level(sk, template) for slot, template in templates.items()},
            quality_long=True,
        )
        max_high = self.profile.preferences.intensity_ratio.high + INTENSITY_TOLERANCE_MESOCYCLE
        budget_s = self.profile.availability.weekly_hours_budget * 3600
        sessions: list[Session] = []
        used: dict[Slot, int] = {}
        for _ in range(300):
            sessions, used = self._assemble(sk, spec, placed, templates, knobs, caps, eves)
            total = sum(s.duration_s for s in sessions)
            key_s = sum(s.duration_s for s in sessions if s.slot in used)
            if (total > knobs.hours_s + 300 or total > budget_s) and self._lower(knobs):
                continue  # key sessions alone exceed the volume target or the weekly budget
            load = sum(weighted_load(s, self.impact) for s in sessions)
            if load > caps.load:
                if total > key_s:
                    knobs.hours_s = min(knobs.hours_s, total) - max(300.0, 0.05 * total)
                    continue
                if self._lower(knobs):
                    continue
            splits = [intensity_split(s, self.zones) for s in sessions]
            high = sum(split[1] for split in splits)
            if total and high / total > max_high:
                if knobs.quality_long and any(s.slot is Slot.LONG for s in sessions):
                    knobs.quality_long = False
                    continue
                if self._lower(knobs):
                    continue
            break
        return sessions, used

    # ------------------------------------------------------------------ entry point
    def build_week(self, sk: WeekSkeleton, history: list[Week]) -> Week:
        spec = GOAL_SPECS[sk.goal.type]
        goal_sport = self.profile.goal_sport(sk.goal)
        if goal_sport not in self.enabled:
            goal_sport = other_sport(goal_sport)
        hours_s = self._volume(sk, history)
        caps = self._caps(sk, history, goal_sport, spec, hours_s)
        picked = self._key_templates(sk, spec, self.profile.goal_sport(sk.goal))
        placed, eves = self._place(sk, history, spec, len(picked))
        self._assign_sports(placed, history, goal_sport, caps, sk.race is not None, eves)
        placed = [p for p in placed if p.sport is not None]
        keys = [p for p in placed if p.slot in KEY_SLOTS]
        for index, p in enumerate(keys):  # renumber after possible drops
            p.slot = KEY_SLOTS[index]
        templates = {p.slot: picked[index] for index, p in enumerate(keys)}

        sessions, used = self._fit(sk, spec, placed, templates, hours_s, caps, eves)
        sessions = [resolve_session(session, self.thresholds) for session in sessions]

        if not sk.recovery and sk.phase not in (Phase.TAPER, Phase.TRANSITION):
            for slot, level in used.items():
                template = templates[slot]
                self.uses[template.id] = max(self.uses.get(template.id, 0), level + 1)
            key = (sk.goal.name, sk.phase)
            self.rotation[key] = self.rotation.get(key, 0) + 1
        races = [goal.name for goal in sk.minor_races]
        if sk.race is not None:
            races.append(sk.race.name)
        return Week(
            index=sk.index,
            start=sk.start,
            phase=sk.phase,
            recovery=sk.recovery,
            goal=sk.goal.name,
            races=races,
            sessions=sessions,
        )
