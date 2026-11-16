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


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
