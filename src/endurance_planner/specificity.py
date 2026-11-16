"""Goal specificity, as data: what each goal type needs, phase by phase.

`keys` gives, per sport and phase, one rotation of template ids per key slot
(KEY_1, KEY_2). The scheduler rotates through each tuple week after week and
skips templates whose prerequisites are not met yet.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from endurance_planner.models.enums import GoalType, Phase, Sport

BASE, BUILD, PEAK = Phase.BASE, Phase.BUILD, Phase.PEAK
Rotations = tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class GoalSpec:
    min_weeks: int  # shortest plan (weeks to race week included) accepted for a first goal
    taper_factors: tuple[float, ...]  # weekly volume vs pre-taper level; last one = race week
    keys: Mapping[Sport, Mapping[Phase, Rotations]]
    long_max_min: Mapping[Sport, int]  # ceiling of the long session
    recommended_hours: float  # typical peak weekly volume; below it a warning is issued
    phase_split: tuple[float, float, float] = (0.45, 0.35, 0.20)  # base, build, peak
    race_pace: float | None = None  # default race pace, fraction of threshold speed (run goals)
    quality_long: frozenset[Phase] = field(default_factory=frozenset)  # phases with a specific long
    back_to_back: bool = False  # second long day in build and peak (ultra)
    technical: bool = False  # one endurance ride per week is flagged as a skills session
    long_notes: str = ""  # practice notes attached to long sessions

    @property
    def race_pace_range(self) -> tuple[float, float]:
        """Accepted goal pace around the default (a target time outside it is clamped)."""
        pace = self.race_pace or 1.0
        return pace - 0.05, pace + 0.05


FUELING_NOTE = (
    "Practice race fueling: 60-90 g of carbohydrate per hour, drink regularly, "
    "use the food and bottles planned for race day."
)

ULTRA_NOTE = (
    "Time in the saddle / on feet matters more than speed: keep it easy. "
    + FUELING_NOTE
    + " Test your kit, lights and pacing plan."
)

GOAL_DISTANCE_KM = {
    GoalType.RUN_5K: 5.0,
    GoalType.RUN_10K: 10.0,
    GoalType.RUN_HALF_MARATHON: 21.0975,
}

_RUN_KEYS_10K: dict[Phase, Rotations] = {
    BASE: (("run_hills", "run_tempo"),),
    BUILD: (("run_tempo", "run_race_pace"), ("run_vo2max", "run_800s")),
    PEAK: (("run_race_pace",), ("run_vo2max", "run_tempo")),
}

GOAL_SPECS: dict[GoalType, GoalSpec] = {
    GoalType.RUN_5K: GoalSpec(
        min_weeks=6,
        taper_factors=(0.60,),
        keys={
            Sport.RUN: {
                BASE: (("run_hills", "run_tempo"),),
                BUILD: (("run_vo2max", "run_800s"), ("run_tempo", "run_hills")),
                PEAK: (("run_800s", "run_vo2max"), ("run_tempo",)),
            }
        },
        long_max_min={Sport.RUN: 80, Sport.BIKE: 180},
        recommended_hours=4,
        race_pace=1.07,
    ),
    GoalType.RUN_10K: GoalSpec(
        min_weeks=8,
        taper_factors=(0.60,),
        keys={Sport.RUN: _RUN_KEYS_10K},
        long_max_min={Sport.RUN: 100, Sport.BIKE: 180},
        recommended_hours=5,
        race_pace=1.03,
    ),
    GoalType.RUN_HALF_MARATHON: GoalSpec(
        min_weeks=10,
        taper_factors=(0.75, 0.55),
        keys={
            Sport.RUN: {
                BASE: (("run_hills", "run_tempo"),),
                BUILD: (("run_tempo",), ("run_race_pace", "run_vo2max")),
                PEAK: (("run_race_pace", "run_tempo"), ("run_vo2max",)),
            }
        },
        long_max_min={Sport.RUN: 130, Sport.BIKE: 180},
        recommended_hours=6,
        race_pace=0.98,
        quality_long=frozenset({BUILD, PEAK}),
    ),
    GoalType.MTB_XC: GoalSpec(
        min_weeks=8,
        taper_factors=(0.60,),
        keys={
            Sport.BIKE: {
                BASE: (("bike_sweet_spot", "bike_low_cadence"),),
                BUILD: (
                    ("bike_threshold", "bike_over_unders"),
                    ("bike_vo2max", "bike_mtb_punches"),
                ),
                PEAK: (
                    ("bike_race_starts", "bike_over_unders"),
                    ("bike_vo2_30_15", "bike_mtb_punches"),
                ),
            }
        },
        long_max_min={Sport.BIKE: 180, Sport.RUN: 75},
        recommended_hours=7,
        technical=True,
    ),
    GoalType.MTB_MARATHON: GoalSpec(
        min_weeks=10,
        taper_factors=(0.75, 0.55),
        keys={
            Sport.BIKE: {
                BASE: (("bike_sweet_spot", "bike_tempo"),),
                BUILD: (("bike_threshold", "bike_sweet_spot"), ("bike_vo2max", "bike_mtb_punches")),
                PEAK: (("bike_over_unders", "bike_threshold"), ("bike_mtb_punches", "bike_vo2max")),
            }
        },
        long_max_min={Sport.BIKE: 300, Sport.RUN: 90},
        recommended_hours=9,
        quality_long=frozenset({BUILD, PEAK}),
        technical=True,
        long_notes=FUELING_NOTE,
    ),
    # Ultra: durability first. One key session a week (tempo / sweet spot, no VO2max block),
    # back-to-back long days in build and peak, fueling practice on every long session.
    GoalType.ULTRA_ENDURANCE: GoalSpec(
        min_weeks=12,
        taper_factors=(0.75, 0.55),
        keys={
            Sport.BIKE: {
                BASE: (("bike_tempo", "bike_low_cadence"),),
                BUILD: (("bike_sweet_spot", "bike_tempo"),),
                PEAK: (("bike_sweet_spot", "bike_threshold"),),
            },
            Sport.RUN: {
                BASE: (("run_hills", "run_tempo"),),
                BUILD: (("run_tempo", "run_hills"),),
                PEAK: (("run_tempo",),),
            },
        },
        long_max_min={Sport.BIKE: 360, Sport.RUN: 180},
        recommended_hours=10,
        phase_split=(0.50, 0.35, 0.15),
        back_to_back=True,
        long_notes=ULTRA_NOTE,
    ),
}
