"""Interval library, as data.

Every template is written in its sport's native unit: fraction of FTP for the
bike, fraction of threshold speed for the run (RPE for effort-based reps such
as hills). A template has a progression ladder: the scheduler moves one rung up
each time the template is used in a loading week.

To add a workout: add a Template here and reference its id in specificity.py.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from endurance_planner.models.enums import Intent, Phase, Sport, StepKind, TargetKind
from endurance_planner.models.plan import Repeat, Session, Step, Target
from endurance_planner.targets import NATIVE_KIND

BASE, BUILD, PEAK, TAPER = Phase.BASE, Phase.BUILD, Phase.PEAK, Phase.TAPER

# Steady-state intensities per sport (native unit).
EASY = {Sport.BIKE: (0.60, 0.72), Sport.RUN: (0.74, 0.82)}
RECOVERY = {Sport.BIKE: (0.45, 0.55), Sport.RUN: (0.66, 0.74)}
WARMUP = {Sport.BIKE: (0.50, 0.70), Sport.RUN: (0.70, 0.80)}
# Default and shortened warm-up / cool-down durations (the short ones are used on tight days).
WARMUP_S = {Sport.BIKE: (900, 600), Sport.RUN: (720, 480)}
COOLDOWN_S = {Sport.BIKE: (600, 300), Sport.RUN: (480, 300)}
# Pace assumed for distance-based reps when the threshold pace is unknown (s/km).
FALLBACK_PACE_S = 300.0

# Quality blocks placed inside long sessions (see long_blocks).
LONG_TEMPO = (0.78, 0.85)  # bike: tempo blocks
LONG_TEMPO_BLOCK_S = 20 * 60
LONG_FINISH = (0.90, 0.95)  # run: marathon-pace finish


@dataclass(frozen=True)
class Seg:
    """One step of a repetition. low/high None = easy recovery at the sport's RECOVERY pace."""

    duration_s: int = 0
    low: float | None = None
    high: float | None = None
    distance_m: int | None = None  # distance-based rep (duration is then estimated)
    rpe: bool = False  # low/high are RPE values (effort-based, e.g. hills)
    goal_pace: bool = False  # replaced by the goal race pace when it is known
    note: str = ""


@dataclass(frozen=True)
class Rung:
    """One level of a progression ladder: sets x (reps x rep) with optional rest between sets."""

    label: str
    reps: int
    rep: tuple[Seg, ...]
    sets: int = 1
    set_rest_s: int = 0


@dataclass(frozen=True)
class Template:
    id: str
    sport: Sport
    intent: Intent
    name: str
    phases: frozenset[Phase]
    ladder: tuple[Rung, ...]
    rationale: str  # one-line physiological rationale
    prerequisites: tuple[str, ...] = ()  # unlocked once any of these has been done
    notes: str = ""
    mtb: bool = False  # best done on the mountain bike when outdoors
    warmup_s: int | None = None  # overrides the sport default


def _fmt(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"


def _fmt_m(metres: int) -> str:
    return f"{metres / 1000:g} km" if metres >= 1000 else f"{metres} m"


def _reps(
    work: tuple[float, float],
    rungs: Sequence[tuple[int, int, int]],
    *,
    distance: bool = False,
    rpe: bool = False,
    goal_pace: bool = False,
    note: str = "",
) -> tuple[Rung, ...]:
    """Ladder of simple intervals; each rung is (reps, work seconds or metres, rest seconds)."""
    ladder = []
    for reps, size, rest_s in rungs:
        seg = Seg(
            duration_s=0 if distance else size,
            distance_m=size if distance else None,
            low=work[0],
            high=work[1],
            rpe=rpe,
            goal_pace=goal_pace,
            note=note,
        )
        label = f"{reps}x{_fmt_m(size) if distance else _fmt(size)}"
        ladder.append(Rung(label=label, reps=reps, rep=(seg, Seg(duration_s=rest_s))))
    return tuple(ladder)


def _sets(
    rep: tuple[Seg, ...], rungs: Sequence[tuple[int, int]], set_rest_s: int, unit: str
) -> tuple[Rung, ...]:
    """Ladder of sets x reps of a fixed repetition; each rung is (sets, reps)."""
    return tuple(
        Rung(label=f"{sets}x{reps}x{unit}", reps=reps, rep=rep, sets=sets, set_rest_s=set_rest_s)
        for sets, reps in rungs
    )


def _all(*phases: Phase) -> frozenset[Phase]:
    return frozenset(phases)


_TEMPLATES = [
    # ---------------------------------------------------------------- bike
    Template(
        id="bike_tempo",
        sport=Sport.BIKE,
        intent=Intent.TEMPO,
        name="Tempo",
        phases=_all(BASE, BUILD),
        ladder=_reps(
            (0.78, 0.86), [(2, 1200, 300), (2, 1500, 300), (3, 1200, 300), (2, 1800, 300)]
        ),
        rationale="Sustained tempo raises aerobic endurance and fatigue resistance at moderate cost.",
    ),
    Template(
        id="bike_low_cadence",
        sport=Sport.BIKE,
        intent=Intent.TEMPO,
        name="Low-cadence force",
        phases=_all(BASE),
        ladder=_reps(
            (0.80, 0.88),
            [(4, 300, 180), (5, 300, 180), (4, 480, 240), (5, 480, 240)],
            note="50-60 rpm, seated, smooth torque",
        ),
        rationale="Low-cadence tempo builds muscular endurance and torque for steep climbs.",
    ),
    Template(
        id="bike_sweet_spot",
        sport=Sport.BIKE,
        intent=Intent.SWEET_SPOT,
        name="Sweet spot",
        phases=_all(BASE, BUILD, PEAK),
        ladder=_reps(
            (0.88, 0.94),
            [
                (3, 600, 300),
                (3, 720, 300),
                (2, 1200, 300),
                (3, 900, 300),
                (4, 900, 300),
                (3, 1200, 300),
            ],
        ),
        rationale="Sweet spot (88-94% FTP) gives a large threshold stimulus for limited fatigue.",
    ),
    Template(
        id="bike_threshold",
        sport=Sport.BIKE,
        intent=Intent.THRESHOLD,
        name="Threshold",
        phases=_all(BUILD, PEAK),
        ladder=_reps(
            (0.95, 1.00),
            [
                (3, 480, 240),
                (3, 600, 300),
                (3, 720, 300),
                (2, 1200, 300),
                (3, 900, 300),
                (4, 720, 300),
            ],
        ),
        rationale="Intervals at FTP extend time at lactate threshold and raise sustainable power.",
    ),
    Template(
        id="bike_over_unders",
        sport=Sport.BIKE,
        intent=Intent.THRESHOLD,
        name="Over-unders",
        phases=_all(BUILD, PEAK),
        ladder=_sets(
            (Seg(120, 1.05, 1.10), Seg(120, 0.88, 0.92)),
            [(2, 3), (3, 3), (3, 4), (4, 3), (4, 4)],
            set_rest_s=300,
            unit="(2 min over + 2 min under)",
        ),
        rationale="Alternating above and below FTP trains lactate clearance under race-like surges.",
        prerequisites=("bike_threshold", "bike_sweet_spot"),
    ),
    Template(
        id="bike_vo2max",
        sport=Sport.BIKE,
        intent=Intent.VO2MAX,
        name="VO2max",
        phases=_all(BUILD, PEAK),
        ladder=_reps(
            (1.08, 1.15),
            [
                (4, 180, 180),
                (5, 180, 180),
                (6, 180, 180),
                (5, 240, 240),
                (4, 300, 300),
                (5, 300, 300),
            ],
        ),
        rationale="3-5 min efforts at 106-120% FTP accumulate time near VO2max to lift the aerobic ceiling.",
        prerequisites=("bike_sweet_spot", "bike_threshold", "bike_tempo"),
    ),
    Template(
        id="bike_vo2_30_15",
        sport=Sport.BIKE,
        intent=Intent.VO2MAX,
        name="VO2max 30/15",
        phases=_all(BUILD, PEAK),
        ladder=_sets(
            (Seg(30, 1.20, 1.30), Seg(15)),
            [(2, 8), (2, 10), (3, 8), (3, 10), (3, 13)],
            set_rest_s=300,
            unit="(30 s on / 15 s off)",
        ),
        rationale="Short on/off intervals keep oxygen uptake high with repeated accelerations.",
        prerequisites=("bike_vo2max",),
    ),
    Template(
        id="bike_mtb_punches",
        sport=Sport.BIKE,
        intent=Intent.ANAEROBIC,
        name="MTB punches",
        phases=_all(BUILD, PEAK),
        ladder=_sets(
            (Seg(45, 1.25, 1.40), Seg(75)),
            [(2, 6), (2, 8), (3, 6), (3, 8), (3, 10)],
            set_rest_s=360,
            unit="(45 s punch / 75 s easy)",
        ),
        rationale="Repeated supra-threshold punches with short recoveries build the repeatability of MTB racing.",
        prerequisites=("bike_sweet_spot", "bike_threshold", "bike_tempo"),
        notes="On the MTB if possible: short steep climbs, out of corners.",
        mtb=True,
    ),
    Template(
        id="bike_race_starts",
        sport=Sport.BIKE,
        intent=Intent.RACE_SPECIFIC,
        name="Race starts",
        phases=_all(PEAK),
        ladder=tuple(
            Rung(
                label=f"{reps}x(2 min start + 8 min settle)",
                reps=reps,
                rep=(Seg(120, 1.20, 1.30), Seg(480, 0.92, 1.00), Seg(360)),
            )
            for reps in (2, 3, 4)
        ),
        rationale="A hard start followed by threshold riding rehearses the first minutes of an MTB race.",
        prerequisites=("bike_threshold", "bike_over_unders"),
        mtb=True,
    ),
    Template(
        id="bike_openers",
        sport=Sport.BIKE,
        intent=Intent.RACE_SPECIFIC,
        name="Openers",
        phases=_all(TAPER),
        ladder=_reps((1.10, 1.20), [(4, 60, 180)]),
        rationale="Short sharp efforts keep the legs primed while taper volume stays low.",
    ),
    # ---------------------------------------------------------------- run
    Template(
        id="run_strides",
        sport=Sport.RUN,
        intent=Intent.ENDURANCE,
        name="Easy run + strides",
        phases=_all(BASE, TAPER),
        ladder=_reps((1.15, 1.25), [(4, 20, 70), (6, 20, 70), (8, 20, 70)]),
        rationale="Strides maintain leg speed and running economy at negligible fatigue.",
        warmup_s=1500,
    ),
    Template(
        id="run_hills",
        sport=Sport.RUN,
        intent=Intent.ANAEROBIC,
        name="Hill repeats",
        phases=_all(BASE, BUILD),
        ladder=_reps(
            (8, 9),
            [(6, 45, 90), (8, 45, 90), (8, 60, 120), (10, 60, 120), (8, 90, 150)],
            rpe=True,
            note="uphill 4-6%, jog back down",
        ),
        rationale="Short hill repeats build strength and power with less impact than flat speed work.",
    ),
    Template(
        id="run_tempo",
        sport=Sport.RUN,
        intent=Intent.THRESHOLD,
        name="Cruise intervals",
        phases=_all(BASE, BUILD, PEAK),
        ladder=_reps(
            (0.97, 1.01),
            [
                (3, 480, 120),
                (3, 600, 120),
                (4, 480, 120),
                (3, 720, 150),
                (2, 1200, 180),
                (3, 900, 180),
            ],
        ),
        rationale="Cruise intervals at threshold pace raise the lactate threshold with controlled stress.",
    ),
    Template(
        id="run_race_pace",
        sport=Sport.RUN,
        intent=Intent.RACE_SPECIFIC,
        name="Race-pace reps",
        phases=_all(BUILD, PEAK),
        ladder=_reps(
            (1.01, 1.05),
            [(5, 1000, 90), (6, 1000, 90), (4, 2000, 150), (3, 3000, 180), (5, 2000, 150)],
            distance=True,
            goal_pace=True,
        ),
        rationale="Reps at goal pace build specific endurance and pacing sense for race day.",
        prerequisites=("run_tempo",),
    ),
    Template(
        id="run_vo2max",
        sport=Sport.RUN,
        intent=Intent.VO2MAX,
        name="VO2max reps",
        phases=_all(BUILD, PEAK),
        ladder=_reps(
            (1.07, 1.11),
            [(5, 120, 120), (6, 120, 120), (5, 180, 180), (6, 180, 180), (5, 240, 210)],
        ),
        rationale="Reps at 3k-5k pace accumulate time near VO2max to lift the aerobic ceiling.",
        prerequisites=("run_tempo", "run_hills"),
    ),
    Template(
        id="run_800s",
        sport=Sport.RUN,
        intent=Intent.VO2MAX,
        name="800 m reps",
        phases=_all(BUILD, PEAK),
        ladder=_reps(
            (1.07, 1.11),
            [(5, 800, 150), (6, 800, 150), (5, 1000, 180), (7, 800, 150), (6, 1000, 180)],
            distance=True,
        ),
        rationale="800-1000 m reps at 5k pace develop VO2max and speed endurance.",
        prerequisites=("run_vo2max", "run_tempo"),
    ),
    Template(
        id="run_openers",
        sport=Sport.RUN,
        intent=Intent.RACE_SPECIFIC,
        name="Openers",
        phases=_all(TAPER),
        ladder=_reps((1.02, 1.06), [(4, 60, 120)], goal_pace=True),
        rationale="Short efforts at race pace keep the legs primed while taper volume stays low.",
    ),
]

TEMPLATES: dict[str, Template] = {template.id: template for template in _TEMPLATES}
OPENERS = {Sport.BIKE: "bike_openers", Sport.RUN: "run_openers"}


def _target(sport: Sport, low: float, high: float, rpe: bool = False) -> Target:
    return Target(kind=TargetKind.RPE if rpe else NATIVE_KIND[sport], low=low, high=high)


def _seg_step(seg: Seg, sport: Sport, pace_s: float | None, goal_pace: float | None) -> Step:
    if seg.low is None or seg.high is None:
        return Step(
            kind=StepKind.RECOVERY,
            duration_s=seg.duration_s,
            target=_target(sport, *RECOVERY[sport]),
        )
    low, high = seg.low, seg.high
    if seg.goal_pace and goal_pace is not None:
        low, high = round(goal_pace - 0.01, 3), round(goal_pace + 0.01, 3)
    target = _target(sport, low, high, seg.rpe)
    duration = seg.duration_s
    if seg.distance_m is not None:
        duration = round(seg.distance_m / 1000 * (pace_s or FALLBACK_PACE_S) / target.mid)
    return Step(duration_s=duration, distance_m=seg.distance_m, target=target, note=seg.note)


def main_set(
    template: Template, level: int, pace_s: float | None = None, goal_pace: float | None = None
) -> list[Step | Repeat]:
    """Main set of a template at a ladder level (clamped to the ladder).

    pace_s: threshold pace, to estimate the duration of distance-based reps.
    goal_pace: goal race pace as a fraction of threshold speed (for goal_pace segments).
    """
    rung = template.ladder[min(level, len(template.ladder) - 1)]
    rep: list[Step | Repeat] = [
        _seg_step(seg, template.sport, pace_s, goal_pace) for seg in rung.rep
    ]
    inner: list[Step | Repeat] = [Repeat(count=rung.reps, steps=rep)] if rung.reps > 1 else rep
    if rung.sets == 1:
        return inner
    rest = Step(
        kind=StepKind.RECOVERY,
        duration_s=rung.set_rest_s,
        target=_target(template.sport, *RECOVERY[template.sport]),
    )
    return [Repeat(count=rung.sets, steps=[*inner, rest])]


def key_blocks(
    template: Template,
    level: int,
    *,
    short: bool = False,
    pace_s: float | None = None,
    goal_pace: float | None = None,
) -> list[Step | Repeat]:
    """Warm-up + main set + cool-down. `short` uses the shortened warm-up and cool-down."""
    sport = template.sport
    warmup_s = WARMUP_S[sport][1 if short else 0]
    if template.warmup_s is not None and not short:
        warmup_s = template.warmup_s
    return [
        Step(kind=StepKind.WARMUP, duration_s=warmup_s, target=_target(sport, *WARMUP[sport])),
        *main_set(template, level, pace_s, goal_pace),
        Step(
            kind=StepKind.COOLDOWN,
            duration_s=COOLDOWN_S[sport][1 if short else 0],
            target=_target(sport, *RECOVERY[sport]),
        ),
    ]


def rung_label(template: Template, level: int) -> str:
    return template.ladder[min(level, len(template.ladder) - 1)].label


_FROM_MARK = re.compile(r" \[from (bike|run)\]$")


def progress_rung(session: Session) -> int | None:
    """Ladder level a session completes, for progression (None: it does not count).

    Schema v2 stores it in Session.rung. For sessions of v1 plans it is read back from the
    name ("<template name> <rung label>", possibly converted to the other sport).
    An amended session without a rung was skipped, shortened or made easier: it does not
    count, so its ladder does not advance.
    """
    if session.rung is not None:
        return session.rung
    if session.amendment_id is not None or session.template_id is None:
        return None
    template = TEMPLATES.get(session.template_id)
    if template is None:
        return None
    name = _FROM_MARK.sub("", session.name)
    levels = [
        lv for lv, rung in enumerate(template.ladder) if name == f"{template.name} {rung.label}"
    ]
    return levels[-1] if levels else None


def steady_blocks(sport: Sport, duration_s: int) -> list[Step | Repeat]:
    """Continuous low-intensity session."""
    return [Step(duration_s=duration_s, target=_target(sport, *EASY[sport]))]


def long_blocks(sport: Sport, duration_s: int, quality: bool) -> list[Step | Repeat]:
    """Long session. With `quality`, long enough sessions get a specific block:
    bike: 20-min tempo blocks in the second half; run: marathon-pace finish.
    """
    easy = _target(sport, *EASY[sport])
    if quality and sport is Sport.BIKE and duration_s >= 2 * 3600:
        count = 2 if duration_s >= 3 * 3600 else 1
        blocks_s = count * (LONG_TEMPO_BLOCK_S + 600)
        lead_s = duration_s - blocks_s
        tempo = Step(duration_s=LONG_TEMPO_BLOCK_S, target=_target(sport, *LONG_TEMPO))
        between = Step(kind=StepKind.RECOVERY, duration_s=600, target=easy)
        lead = Step(duration_s=lead_s, target=easy)
        if count == 1:
            return [lead, tempo, between]
        return [lead, Repeat(count=count, steps=[tempo, between])]
    if quality and sport is Sport.RUN and duration_s >= 55 * 60:
        finish_s = 20 * 60 if duration_s >= 70 * 60 else 15 * 60
        return [
            Step(duration_s=duration_s - finish_s, target=easy),
            Step(
                duration_s=finish_s, target=_target(sport, *LONG_FINISH), note="steady, not all-out"
            ),
        ]
    return [Step(duration_s=duration_s, target=easy)]
