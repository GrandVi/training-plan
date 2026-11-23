"""Strength training, as data: exercise library, phase templates with ladders, session builder.

Strength is not a Sport: sessions live in Week.strength, count in the weekly time budget, and
stay out of the endurance load and its ramp caps (a flat estimate feeds the CTL projection
only, see load.STRENGTH_TSS_PER_HOUR).

Prescriptions are sets x reps at a target reps in reserve (RIR), never a % of a 1RM nobody
measured. For each movement pattern the builder takes the first exercise of the library whose
equipment is available that day, so a plan degrades cleanly to bodyweight. Build and peak use
the harder variant of an exercise (its progression chain) when its equipment is there. No
jumps or plyometrics; the higher-impact exercises (`high_impact`) are left out when
athlete.injury_notes is set. Generic guidance only: this is not injury or medical advice.
"""

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations, pairwise

from endurance_planner.models.enums import (
    Equipment,
    Pattern,
    Phase,
    Priority,
    Severity,
    Slot,
    Sport,
)
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Exercise, Session, StrengthSession, Week
from endurance_planner.models.profile import Goal, Profile, StrengthSettings

BW, DB, BB, KB = Equipment.BODYWEIGHT, Equipment.DUMBBELLS, Equipment.BARBELL, Equipment.KETTLEBELL
BANDS, BAR, MACHINES = Equipment.BANDS, Equipment.PULLUP_BAR, Equipment.MACHINES
SQUAT, HINGE, SINGLE_LEG = Pattern.SQUAT, Pattern.HINGE, Pattern.SINGLE_LEG
PUSH, PULL, CALF, CORE = Pattern.PUSH, Pattern.PULL, Pattern.CALF, Pattern.CORE


@dataclass(frozen=True)
class ExerciseSpec:
    id: str
    name: str
    pattern: Pattern
    equipment: tuple[Equipment, ...]  # any of these; the first available one is used
    rationale: str
    unilateral: bool = False
    hold: bool = False  # prescribed in seconds
    high_impact: bool = False  # left out when athlete.injury_notes is set
    harder: str | None = None  # progression chain: variant used in build and peak


# Order matters: for each pattern the first exercise with available equipment is chosen.
LIBRARY: tuple[ExerciseSpec, ...] = (
    # squat
    ExerciseSpec(
        "goblet_squat",
        "Goblet squat",
        SQUAT,
        (DB, KB),
        "Front-loaded squat: grooves depth and trunk position with moderate loads.",
        harder="back_squat",
    ),
    ExerciseSpec(
        "back_squat",
        "Back squat",
        SQUAT,
        (BB,),
        "Heavy bilateral squat: quadriceps and glute force for climbs and late-race power.",
    ),
    ExerciseSpec(
        "leg_press", "Leg press", SQUAT, (MACHINES,), "Squat pattern without spinal loading."
    ),
    ExerciseSpec(
        "bodyweight_squat",
        "Bodyweight squat, 3 s down",
        SQUAT,
        (BW,),
        "Slow lowering: time under tension replaces load.",
        harder="single_leg_box_squat",
    ),
    ExerciseSpec(
        "single_leg_box_squat",
        "Single-leg box squat",
        SQUAT,
        (BW,),
        "Squat on one leg to a box: real strength work without equipment.",
        unilateral=True,
    ),
    # hinge
    ExerciseSpec(
        "romanian_deadlift",
        "Romanian deadlift",
        HINGE,
        (BB, DB),
        "Hamstrings and glutes under load: hip extension for climbing and running economy.",
    ),
    ExerciseSpec(
        "kettlebell_deadlift",
        "Kettlebell deadlift",
        HINGE,
        (KB,),
        "Hip hinge with a manageable load.",
    ),
    ExerciseSpec(
        "band_good_morning",
        "Band good morning",
        HINGE,
        (BANDS,),
        "Hip hinge against band tension.",
    ),
    ExerciseSpec(
        "glute_bridge",
        "Glute bridge",
        HINGE,
        (BW,),
        "Hip extension strength without equipment.",
        harder="single_leg_glute_bridge",
    ),
    ExerciseSpec(
        "single_leg_glute_bridge",
        "Single-leg glute bridge",
        HINGE,
        (BW,),
        "Harder bridge: one leg carries the load.",
        unilateral=True,
    ),
    # single leg
    ExerciseSpec(
        "step_up",
        "Dumbbell step-up",
        SINGLE_LEG,
        (DB, KB),
        "Single-leg drive onto a box, close to climbing on foot or out of the saddle.",
        unilateral=True,
        harder="rfe_split_squat",
    ),
    ExerciseSpec(
        "rfe_split_squat",
        "Rear-foot-elevated split squat",
        SINGLE_LEG,
        (DB, KB, BB),
        "Heavy single-leg strength and hip stability.",
        unilateral=True,
    ),
    ExerciseSpec(
        "walking_lunge",
        "Walking lunge",
        SINGLE_LEG,
        (BW,),
        "Single-leg strength with deceleration, close to running demands.",
        unilateral=True,
        high_impact=True,
        harder="rfe_split_squat_bw",
    ),
    ExerciseSpec(
        "split_squat",
        "Split squat",
        SINGLE_LEG,
        (BW,),
        "Single-leg strength and balance from a stable stance.",
        unilateral=True,
        harder="rfe_split_squat_bw",
    ),
    ExerciseSpec(
        "rfe_split_squat_bw",
        "Rear-foot-elevated split squat, bodyweight",
        SINGLE_LEG,
        (BW,),
        "Harder single-leg squat with the rear foot on a bench.",
        unilateral=True,
    ),
    # push
    ExerciseSpec(
        "dumbbell_press",
        "Dumbbell bench press",
        PUSH,
        (DB,),
        "Pushing strength with free shoulder movement.",
        harder="bench_press",
    ),
    ExerciseSpec(
        "bench_press",
        "Bench press",
        PUSH,
        (BB,),
        "Upper-body pushing strength for posture and bike handling.",
    ),
    ExerciseSpec(
        "machine_press", "Machine chest press", PUSH, (MACHINES,), "Guided pushing strength."
    ),
    ExerciseSpec(
        "push_up",
        "Push-up",
        PUSH,
        (BW,),
        "Pushing strength and trunk control without equipment.",
        harder="feet_elevated_push_up",
    ),
    ExerciseSpec(
        "feet_elevated_push_up",
        "Feet-elevated push-up",
        PUSH,
        (BW,),
        "Harder push-up with the feet on a bench.",
    ),
    # pull
    ExerciseSpec(
        "one_arm_row",
        "One-arm dumbbell row",
        PULL,
        (DB, KB),
        "Upper-back strength, one side at a time.",
        unilateral=True,
        harder="barbell_row",
    ),
    ExerciseSpec(
        "barbell_row",
        "Barbell row",
        PULL,
        (BB,),
        "Upper-back strength for posture on long rides and runs.",
    ),
    ExerciseSpec("pull_up", "Pull-up", PULL, (BAR,), "Vertical pulling strength."),
    ExerciseSpec("lat_pulldown", "Lat pulldown", PULL, (MACHINES,), "Guided vertical pull."),
    ExerciseSpec("band_row", "Band row", PULL, (BANDS,), "Horizontal pull against band tension."),
    ExerciseSpec(
        "prone_ytw",
        "Prone Y-T-W raises",
        PULL,
        (BW,),
        "Shoulder and upper-back endurance without equipment.",
    ),
    # calf
    ExerciseSpec(
        "weighted_calf_raise",
        "Standing calf raise, weighted",
        CALF,
        (DB, BB, KB, MACHINES),
        "Calf and Achilles capacity for running and climbing.",
    ),
    ExerciseSpec(
        "single_leg_calf_raise",
        "Single-leg calf raise, 3 s down",
        CALF,
        (BW,),
        "Calf strength one leg at a time, slow lowering.",
        unilateral=True,
    ),
    # core
    ExerciseSpec(
        "pallof_press",
        "Pallof press",
        CORE,
        (BANDS, MACHINES),
        "Anti-rotation trunk strength.",
        unilateral=True,
    ),
    ExerciseSpec(
        "dead_bug",
        "Dead bug",
        CORE,
        (BW,),
        "Trunk control while the limbs move.",
        harder="side_plank",
    ),
    ExerciseSpec(
        "side_plank",
        "Side plank",
        CORE,
        (BW,),
        "Lateral trunk strength for stability on uneven ground.",
        unilateral=True,
        hold=True,
    ),
)
EXERCISES: dict[str, ExerciseSpec] = {spec.id: spec for spec in LIBRARY}


@dataclass(frozen=True)
class Rung:
    sets: int
    reps: int
    rir: int

    @property
    def label(self) -> str:
        return f"{self.sets}x{self.reps}"


@dataclass(frozen=True)
class StrengthTemplate:
    id: str
    name: str
    ladder: tuple[Rung, ...]
    set_minutes: float  # one main set with its rest
    rationale: str
    harder: bool = False  # exercises use their harder variant (progression chain)


TEMPLATES: dict[str, StrengthTemplate] = {
    t.id: t
    for t in (
        StrengthTemplate(
            "strength_base",
            "Strength foundation",
            (Rung(2, 15, 4), Rung(3, 15, 4), Rung(3, 12, 3), Rung(3, 10, 3)),
            2.0,
            "Foundation strength: moderate loads and many reps, far from failure, to build "
            "tissue tolerance before heavier work.",
        ),
        StrengthTemplate(
            "strength_build",
            "Strength build",
            (Rung(3, 8, 3), Rung(4, 6, 2), Rung(4, 5, 2), Rung(5, 4, 1)),
            3.0,
            "Heavier sets with fewer reps raise maximal strength, which helps running economy "
            "and pedalling force with little added fatigue.",
            harder=True,
        ),
        StrengthTemplate(
            "strength_peak",
            "Strength maintenance",
            (Rung(1, 5, 2), Rung(2, 4, 2)),
            3.0,
            "One or two heavy sets per pattern keep the strength gained while race-specific "
            "endurance work takes priority.",
            harder=True,
        ),
        StrengthTemplate(
            "strength_light",
            "Light strength",
            (Rung(1, 8, 4),),
            2.0,
            "A short, light session keeps the movement patterns without adding fatigue.",
        ),
    )
}
PHASE_TEMPLATE = {
    Phase.BASE: "strength_base",
    Phase.BUILD: "strength_build",
    Phase.PEAK: "strength_peak",
    Phase.TAPER: "strength_light",
    Phase.TRANSITION: "strength_light",
}
# Sessions a week by phase; availability.strength.sessions_per_week is a cap.
SESSIONS_PER_PHASE = {
    Phase.BASE: 2,
    Phase.BUILD: 2,
    Phase.PEAK: 1,
    Phase.TAPER: 1,
    Phase.TRANSITION: 1,
}
# Movement patterns of each session variant: (main patterns, accessory patterns). With two
# sessions a week they alternate A and B; a single session covers the full body.
VARIANTS: dict[str, tuple[tuple[Pattern, ...], tuple[Pattern, ...]]] = {
    "A": ((SQUAT, HINGE, PUSH), (CALF, CORE)),
    "B": ((SINGLE_LEG, HINGE, PULL), (CALF, CORE)),
    "full": ((SQUAT, HINGE, SINGLE_LEG, PUSH, PULL), (CALF, CORE)),
}
VARIANT_WHY = {
    "A": "Squat, hinge and push day.",
    "B": "Single-leg, hinge and pull day.",
    "full": "Full-body session: every movement pattern once.",
}
ACCESSORY_REPS = {CALF: 15, CORE: 10}  # at RIR 2 or more; holds use HOLD_SECONDS
ACCESSORY_MAX_SETS = 3
HOLD_SECONDS = 40
ACCESSORY_SET_MINUTES = 1.0
UNILATERAL_FACTOR = 1.5  # a set done on each side takes longer
WARMUP_MINUTES = 5
RECOVERY_VOLUME = 0.6  # recovery weeks: about 40% fewer sets
MIN_SESSION_MINUTES = 20  # a session that cannot fit this is dropped
MIN_MAIN_EXERCISES = 2  # a shortened session keeps at least this many main exercises
DISCLAIMER = "Generic strength guidance, not injury advice: stop any exercise that hurts."


def exercise_equipment(spec: ExerciseSpec, available: set[Equipment]) -> Equipment | None:
    """Equipment the exercise uses on a day with `available` (None: it cannot be done)."""
    return next((item for item in spec.equipment if item in available), None)


def allowed(spec: ExerciseSpec, available: set[Equipment], injury: bool) -> bool:
    return exercise_equipment(spec, available) is not None and not (injury and spec.high_impact)


def select_exercise(
    pattern: Pattern, available: set[Equipment], *, injury: bool, harder: bool = False
) -> ExerciseSpec | None:
    """First exercise of the pattern that is allowed, or its harder variant when `harder`
    and that variant is allowed too. Every pattern has a bodyweight exercise, so None only
    comes with an empty equipment set."""
    for spec in LIBRARY:
        if spec.pattern is not pattern or not allowed(spec, available, injury):
            continue
        hard = EXERCISES[spec.harder] if harder and spec.harder else None
        return hard if hard is not None and allowed(hard, available, injury) else spec
    return None


def _exercise(spec: ExerciseSpec, sets: int, reps: int, rir: int, accessory: bool) -> Exercise:
    return Exercise(
        id=spec.id,
        name=spec.name,
        pattern=spec.pattern,
        sets=sets,
        reps=HOLD_SECONDS if spec.hold else reps,
        unit="s" if spec.hold else "reps",
        rir=None if spec.hold else rir,
        per_side=spec.unilateral,
        accessory=accessory,
    )


def estimate_minutes(exercises: list[Exercise], template: StrengthTemplate) -> float:
    """Warm-up plus every set with its rest (a set on each side takes longer)."""
    total = float(WARMUP_MINUTES)
    for exercise in exercises:
        per_set = ACCESSORY_SET_MINUTES if exercise.accessory else template.set_minutes
        total += exercise.sets * per_set * (UNILATERAL_FACTOR if exercise.per_side else 1.0)
    return total


def fit(exercises: list[Exercise], template: StrengthTemplate, minutes: float) -> list[Exercise]:
    """The exercises shortened to fit `minutes`: accessories go first (the last one first),
    then one set at a time from the main exercises with the most sets (later ones first),
    down to one set each, then the last main exercises (MIN_MAIN_EXERCISES stay)."""
    kept = [exercise.model_copy() for exercise in exercises]
    while estimate_minutes(kept, template) > minutes:
        accessories = [e for e in kept if e.accessory]
        reducible = [index for index, e in enumerate(kept) if e.sets > 1]
        if accessories:
            kept.remove(accessories[-1])
        elif reducible:
            kept[max(reducible, key=lambda i: (kept[i].sets, i))].sets -= 1
        elif len(kept) > MIN_MAIN_EXERCISES:
            kept.pop()
        else:
            break
    return kept


def session_minutes(exercises: list[Exercise], template: StrengthTemplate) -> int:
    """Planned minutes: the estimate rounded up to 5 min, at least MIN_SESSION_MINUTES."""
    estimate = estimate_minutes(exercises, template)
    return max(MIN_SESSION_MINUTES, 5 * math.ceil(estimate / 5 - 1e-9))


def variant_of(position: int, count: int) -> str:
    """Variant of the session at `position` (0-based) of `count` sessions in a week."""
    if count <= 1:
        return "full"
    return "A" if position % 2 == 0 else "B"


@dataclass(frozen=True)
class SessionRequest:
    """What to build: the scheduler decides date, template, level and the time limit."""

    date: dt.date
    phase: Phase
    template_id: str
    level: int
    variant: str
    recovery: bool
    equipment: frozenset[Equipment]
    injury: bool
    minutes: int  # most minutes the session may take


def prescription(template_id: str, level: int, recovery: bool) -> tuple[int, int, int]:
    """(sets, reps, rir) of the main exercises at a ladder level."""
    template = TEMPLATES[template_id]
    rung = template.ladder[min(level, len(template.ladder) - 1)]
    if not recovery:
        return rung.sets, rung.reps, rung.rir
    return max(1, round(rung.sets * RECOVERY_VOLUME)), rung.reps, min(rung.rir + 1, 5)


def build_session(request: SessionRequest) -> StrengthSession | None:
    """A strength session at a ladder level, shortened to fit `request.minutes` (accessories
    first). None if the time is below MIN_SESSION_MINUTES or even the shortest version does
    not fit."""
    if request.minutes < MIN_SESSION_MINUTES:
        return None
    template = TEMPLATES[request.template_id]
    level = min(request.level, len(template.ladder) - 1)
    available = set(request.equipment) | {BW}
    sets, reps, rir = prescription(template.id, level, request.recovery)
    main, accessories = VARIANTS[request.variant]
    exercises: list[Exercise] = []
    for pattern in main:
        spec = select_exercise(pattern, available, injury=request.injury, harder=template.harder)
        if spec is not None:
            exercises.append(_exercise(spec, sets, reps, rir, accessory=False))
    for pattern in accessories:
        spec = select_exercise(pattern, available, injury=request.injury)
        if spec is not None:
            accessory_sets = min(sets, ACCESSORY_MAX_SETS)
            exercises.append(
                _exercise(spec, accessory_sets, ACCESSORY_REPS[pattern], max(rir, 2), True)
            )
    exercises = fit(exercises, template, request.minutes)
    if not exercises or estimate_minutes(exercises, template) > request.minutes:
        return None
    used = {exercise_equipment(EXERCISES[e.id], available) for e in exercises}
    why = f"{template.rationale} {VARIANT_WHY[request.variant]}"
    if request.recovery:
        why += " Recovery week: about 40% fewer sets."
    suffix = " (recovery week)" if request.recovery else ""
    return StrengthSession(
        date=request.date,
        name=f"{template.name} {request.variant}: {template.ladder[level].label}{suffix}",
        phase=request.phase,
        template_id=template.id,
        variant=request.variant,
        exercises=exercises,
        minutes=min(request.minutes, session_minutes(exercises, template)),
        rationale=f"{why} {DISCLAIMER}",
        equipment_used=[item for item in Equipment if item in used],
        rung=level,
    )


def exercise_lines(session: StrengthSession) -> list[str]:
    """One numbered line per exercise ("1. Goblet squat: 3 x 12 reps, 3 reps in reserve")."""
    return [f"{index}. {exercise.label()}" for index, exercise in enumerate(session.exercises, 1)]


def description(session: StrengthSession) -> str:
    """Plain-text description (intervals.icu workout text has no sets and reps; no line
    starts with "-", which it would read as a workout step)."""
    lines = exercise_lines(session)
    if session.equipment_used:
        lines.append("Equipment: " + ", ".join(item.value for item in session.equipment_used))
    return "\n".join([*lines, "", session.rationale])


# --- placement --------------------------------------------------------------------------
STRENGTH_MIN_GAP_DAYS = 2  # two strength sessions at least 48 h apart
A_RACE_FREE_DAYS = 3  # no strength in the last 3 days before an A race
LONG_SLOTS = (Slot.LONG, Slot.LONG_2)
# Why a day cannot take a strength session (codes of day_problem), and the validation code
# each one is reported under.
DAY_PROBLEMS = {
    "off": ("strength_placement", "the day is off (calendar exception)"),
    "race": ("strength_race_proximity", "it is a race day or the day before a race"),
    "a_race": (
        "strength_race_proximity",
        f"it is in the last {A_RACE_FREE_DAYS} days before an A race",
    ),
    "hard_or_long": ("strength_placement", "a hard or long session is planned that day"),
    "before_hard_or_long": ("strength_placement", "a hard or long session follows the next day"),
    "rest_day": (
        "strength_placement",
        "a rest day (no training time that day) and availability.strength.allow_extra_days "
        "is false",
    ),
}


def heavy(session: Session) -> bool:
    """Hard or long endurance session: no strength that day nor the day before."""
    return session.intent.hard or session.slot in LONG_SLOTS


def weekly_count(phase: Phase, settings: StrengthSettings) -> int:
    """Strength sessions planned in a week of `phase` (the phase table, capped by the profile)."""
    return min(SESSIONS_PER_PHASE[phase], settings.sessions_per_week)


def reserved_s(phase: Phase, settings: StrengthSettings) -> int:
    """Weekly time kept free for strength when the endurance volume is set."""
    return weekly_count(phase, settings) * settings.minutes * 60


def loading(week: Week) -> bool:
    """Weeks whose completed sessions advance the strength ladders."""
    return not week.recovery and week.phase not in (Phase.TAPER, Phase.TRANSITION)


def training_day(profile: Profile, date: dt.date) -> bool:
    """The date has training time (weekly pattern or calendar exception)."""
    return any(profile.availability.minutes(date, sport) > 0 for sport in Sport)


def day_problems(
    date: dt.date,
    sessions: Mapping[dt.date, Session],
    profile: Profile,
    goals: Iterable[Goal],
    explicit: bool = False,
) -> list[str]:
    """Why `date` cannot take a strength session (DAY_PROBLEMS keys; empty: it can).

    sessions: endurance sessions by date. explicit: an amended session, which may be on a
    rest day (the athlete chose it). A training day without an endurance session that week
    is not a rest day; the weekly number of training days is checked separately."""
    availability = profile.availability
    problems: list[str] = []
    if availability.off(date):
        problems.append("off")
    for goal in goals:
        days = (goal.date - date).days
        if days in (0, 1):
            problems.append("race")
        elif goal.priority is Priority.A and 0 < days <= A_RACE_FREE_DAYS:
            problems.append("a_race")
    same_day = sessions.get(date)
    if same_day is not None and heavy(same_day):
        problems.append("hard_or_long")
    next_day = sessions.get(date + dt.timedelta(days=1))
    if next_day is not None and heavy(next_day):
        problems.append("before_hard_or_long")
    extra_ok = availability.strength.allow_extra_days or explicit
    if same_day is None and not extra_ok and not training_day(profile, date):
        problems.append("rest_day")
    return list(dict.fromkeys(problems))


def training_days(week: Week, explicit: bool = True) -> set[dt.date]:
    """Days of the week with an endurance or strength session (amended strength sessions are
    left out when `explicit` is False: the athlete chose those days)."""
    days = {session.date for session in week.sessions}
    return days | {s.date for s in week.strength if explicit or s.amendment_id is None}


@dataclass(frozen=True)
class StrengthPins:
    """Fixed strength content of a week (see replan.py): sessions kept as they are (done or
    amended), dates of skipped sessions (not replaced: no catch-up), first date open."""

    sessions: tuple[StrengthSession, ...] = ()
    skipped: tuple[dt.date, ...] = ()
    not_before: dt.date | None = None


@dataclass
class StrengthPlanner:
    """Places strength sessions in weeks whose endurance sessions are already planned.

    Days: after an easy endurance session, or on another training day of the week while the
    week stays within days_per_week (rest days and more days only with allow_extra_days);
    never on or the day before a hard or long session, never on or the day before a race,
    none in the last A_RACE_FREE_DAYS days before an A race, at least STRENGTH_MIN_GAP_DAYS
    apart. Days after an easy session come first, then the widest spacing, then the earliest
    days.
    Time: strength minutes count in the weekly budget: sessions are shortened (accessories
    first), then dropped, with a warning. Ladders advance once per loading week, from the
    rung of its sessions; a skipped session or one without a rung (amended under 80% of its
    minutes) holds the rung.
    """

    profile: Profile
    goals: Sequence[Goal]
    uses: dict[str, int] = field(default_factory=dict)  # template id -> next ladder level
    held: set[dt.date] = field(default_factory=set)  # dates of skipped sessions (rung held)
    shortened: list[int] = field(default_factory=list)  # week indexes
    dropped: dict[int, int] = field(default_factory=dict)  # week index -> sessions dropped

    @property
    def settings(self) -> StrengthSettings:
        return self.profile.availability.strength

    def seed(self, weeks: Iterable[Week]) -> None:
        """Ladder state reached after `weeks` (kept as they are)."""
        for week in weeks:
            self._advance(week)

    def _advance(self, week: Week) -> None:
        """A loading week moves its ladder one rung up, unless one of its sessions was
        skipped or not completed (rung None): then the rung is held."""
        if not loading(week) or not week.strength:
            return
        end = week.start + dt.timedelta(days=6)
        if any(week.start <= day <= end for day in self.held):
            return
        if any(session.rung is None for session in week.strength):
            return
        for session in week.strength:
            done = self.uses.get(session.template_id, 0)
            self.uses[session.template_id] = max(done, (session.rung or 0) + 1)

    def place(
        self,
        weeks: list[Week],
        first: int = 0,
        last: int | None = None,
        pins: Mapping[int, StrengthPins] | None = None,
    ) -> list[Week]:
        """`weeks` with strength sessions placed in weeks first..last (the others are kept;
        call seed() with the weeks before `first` beforehand)."""
        last = len(weeks) - 1 if last is None else last
        pins = pins or {}
        sessions = {s.date: s for week in weeks for s in week.sessions}
        result = list(weeks)
        for index in range(first, last + 1):
            week = result[index]
            if self.settings.sessions_per_week or week.strength:
                neighbours = [
                    s.date
                    for other in (index - 1, index + 1)
                    if 0 <= other < len(result)
                    for s in result[other].strength
                ]
                strength = self._week(week, sessions, pins.get(index, StrengthPins()), neighbours)
                result[index] = week.model_copy(update={"strength": strength})
            self._advance(result[index])
        return result

    def _week(
        self,
        week: Week,
        sessions: Mapping[dt.date, Session],
        pins: StrengthPins,
        neighbours: list[dt.date],
    ) -> list[StrengthSession]:
        kept = list(pins.sessions)
        count = weekly_count(week.phase, self.settings) - len(kept) - len(pins.skipped)
        if count <= 0:
            return kept
        taken = {s.date for s in kept} | set(pins.skipped)
        candidates = [
            date
            for date in (week.start + dt.timedelta(days=offset) for offset in range(7))
            if date not in taken
            and (pins.not_before is None or date >= pins.not_before)
            and not day_problems(date, sessions, self.profile, self.goals)
        ]
        fixed = sorted([*taken, *neighbours])
        extra_days = len(candidates)  # strength-only days the week may still take
        if not self.settings.allow_extra_days:
            used = {s.date for s in week.sessions} | {s.date for s in kept}
            extra_days = max(0, self.profile.availability.days_per_week - len(used))
        dates = self._choose(candidates, count, fixed, sessions, extra_days)
        if len(dates) < count:
            self.dropped[week.index] = self.dropped.get(week.index, 0) + count - len(dates)
        budget = self.profile.availability.weekly_hours_budget * 60
        free = budget - week.duration_s / 60 - sum(s.minutes for s in kept)
        placed = self._build(week, list(dates), free, sorted(taken))
        return sorted([*kept, *placed], key=lambda s: s.date)

    @staticmethod
    def _choose(
        candidates: list[dt.date],
        count: int,
        fixed: list[dt.date],
        sessions: Mapping[dt.date, Session],
        extra_days: int,
    ) -> tuple[dt.date, ...]:
        """Dates for up to `count` sessions: at most `extra_days` without an endurance
        session, STRENGTH_MIN_GAP_DAYS apart (fixed dates included); days after an endurance
        session first, then the widest spacing, then the earliest dates."""
        for size in range(min(count, len(candidates)), 0, -1):
            best: tuple[tuple[int, int, tuple[dt.date, ...]], tuple[dt.date, ...]] | None = None
            for combo in combinations(candidates, size):
                dates = sorted([*combo, *fixed])
                gaps = [(b - a).days for a, b in pairwise(dates)]
                if any(gap < STRENGTH_MIN_GAP_DAYS for gap in gaps):
                    continue
                strength_only = sum(date not in sessions for date in combo)
                if strength_only > extra_days:
                    continue
                key = (strength_only, -min(gaps, default=7), combo)
                if best is None or key < best[0]:
                    best = (key, combo)
            if best is not None:
                return best[1]
        return ()

    def _build(
        self, week: Week, dates: list[dt.date], free_min: float, taken: list[dt.date]
    ) -> list[StrengthSession]:
        """Sessions on `dates`, shortened then dropped (last first) to fit `free_min`.
        Variants (A, B or full) follow the order of all the week's dates, `taken` ones
        (kept or skipped sessions) included, so a skipped session is not made up."""
        minutes = self.settings.minutes
        while dates:
            order = sorted([*taken, *dates])
            built = [
                self._session(week, date, order.index(date), len(order), minutes) for date in dates
            ]
            sessions = [session for session in built if session is not None]
            if len(sessions) == len(dates) and sum(s.minutes for s in sessions) <= free_min:
                if minutes < self.settings.minutes:
                    self.shortened.append(week.index)
                return sessions
            if len(sessions) == len(dates):  # over budget: shorter sessions
                minutes = min(minutes - 5, int(free_min / len(dates) // 5 * 5))
                continue
            dates.pop()  # too short to be useful: one session less
            self.dropped[week.index] = self.dropped.get(week.index, 0) + 1
            minutes = self.settings.minutes
        return []

    def _session(
        self, week: Week, date: dt.date, position: int, total: int, minutes: int
    ) -> StrengthSession | None:
        template_id = PHASE_TEMPLATE[week.phase]
        level = self.uses.get(template_id, 0)
        if not loading(week):  # recovery, taper, transition: the last rung done, not a new one
            level = max(0, level - 1)
        return build_session(
            SessionRequest(
                date=date,
                phase=week.phase,
                template_id=template_id,
                level=level,
                variant=variant_of(position, total),
                recovery=week.recovery,
                equipment=frozenset(self.profile.availability.equipment(date)),
                injury=bool(self.profile.athlete.injury_notes.strip()),
                minutes=minutes,
            )
        )

    def issues(self) -> list[Issue]:
        """Warnings about the sessions shortened or dropped."""
        issues: list[Issue] = []
        if self.shortened:
            weeks = ", ".join(str(index) for index in sorted(set(self.shortened)))
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    code="strength_shortened",
                    message=f"strength sessions shortened to fit the weekly hours budget "
                    f"(weeks {weeks}); accessory exercises go first",
                )
            )
        if self.dropped:
            weeks = ", ".join(str(index) for index in sorted(self.dropped))
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    code="strength_dropped",
                    message=f"{sum(self.dropped.values())} strength session(s) not planned "
                    f"(weeks {weeks}): no day without a hard or long session the next day, "
                    "a race nearby, or no time left in the weekly hours budget",
                )
            )
        return issues
