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
from dataclasses import dataclass

from endurance_planner.models.enums import Equipment, Pattern, Phase
from endurance_planner.models.plan import Exercise, StrengthSession

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


def description(session: StrengthSession) -> str:
    """Plain-text description (intervals.icu workout text has no sets and reps)."""
    lines = [f"- {exercise.label()}" for exercise in session.exercises]
    if session.equipment_used:
        lines.append("Equipment: " + ", ".join(item.value for item in session.equipment_used))
    return "\n".join([*lines, "", session.rationale])
