"""Shared enumerations."""

from enum import StrEnum


class Sport(StrEnum):
    BIKE = "bike"
    RUN = "run"


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class Experience(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class GoalType(StrEnum):
    RUN_5K = "run_5k"
    RUN_10K = "run_10k"
    RUN_HALF_MARATHON = "run_half_marathon"
    MTB_XC = "mtb_xc"
    MTB_MARATHON = "mtb_marathon"
    ULTRA_ENDURANCE = "ultra_endurance"


class Priority(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class TargetKind(StrEnum):
    """Unit of a relative target (see models.plan.Target)."""

    POWER = "power"  # fraction of FTP
    PACE = "pace"  # fraction of threshold speed (1.05 = 5% faster than threshold pace)
    HR = "hr"  # fraction of LTHR
    RPE = "rpe"  # 1-10 scale


class StepKind(StrEnum):
    WARMUP = "warmup"
    WORK = "work"
    RECOVERY = "recovery"
    COOLDOWN = "cooldown"


class Slot(StrEnum):
    """Sport-agnostic role of a session in the week."""

    KEY_1 = "key_1"
    KEY_2 = "key_2"
    KEY_3 = "key_3"
    LONG = "long"
    LONG_2 = "long_2"  # second day of a back-to-back
    EASY = "easy"


class Intent(StrEnum):
    """Physiological intent, preserved when a session is swapped to the other sport."""

    RECOVERY = "recovery"
    ENDURANCE = "endurance"
    TEMPO = "tempo"
    SWEET_SPOT = "sweet_spot"
    THRESHOLD = "threshold"
    VO2MAX = "vo2max"
    ANAEROBIC = "anaerobic"
    RACE_SPECIFIC = "race_specific"
    TECHNICAL = "technical"

    @property
    def hard(self) -> bool:
        """Counts as a hard (quality) session for the weekly limit and spacing rules."""
        return self.value in _HARD_INTENTS


_HARD_INTENTS = {"tempo", "sweet_spot", "threshold", "vo2max", "anaerobic", "race_specific"}


class Phase(StrEnum):
    BASE = "base"
    BUILD = "build"
    PEAK = "peak"
    TAPER = "taper"
    TRANSITION = "transition"


class ActivityType(StrEnum):
    """intervals.icu activity type."""

    RIDE = "Ride"
    MTB = "MountainBikeRide"
    RUN = "Run"
    WEIGHT_TRAINING = "WeightTraining"


class BikeActivity(StrEnum):
    """Kind of bike session a day allows (calendar exceptions, amendments)."""

    ROAD = "road"
    MTB = "mtb"
    TRAINER = "trainer"


class Equipment(StrEnum):
    """Strength equipment; bodyweight is always available."""

    BODYWEIGHT = "bodyweight"
    DUMBBELLS = "dumbbells"
    BARBELL = "barbell"
    KETTLEBELL = "kettlebell"
    BANDS = "bands"
    PULLUP_BAR = "pullup_bar"
    MACHINES = "machines"


class Pattern(StrEnum):
    """Movement pattern of a strength exercise."""

    SQUAT = "squat"
    HINGE = "hinge"
    SINGLE_LEG = "single_leg"
    PUSH = "push"
    PULL = "pull"
    CALF = "calf"
    CORE = "core"


class Scope(StrEnum):
    """How much of a running plan an amendment rebuilds (see amend.py)."""

    SESSION = "session"  # only the amended session; its week is re-validated
    WEEK = "week"  # also the other (unpinned) sessions of that week
    REST = "rest"  # also every later week


class AmendmentKind(StrEnum):
    SESSION = "session"  # one session edited, skipped or moved
    UNAVAILABLE = "unavailable"  # a date range with less (or no) time
    STRENGTH = "strength"  # one strength session shortened, skipped, moved or re-equipped


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
