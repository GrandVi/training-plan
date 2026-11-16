"""Zone models (as data), zone tables, and HR/RPE <-> intensity mappings.

All bounds are fractions of the threshold anchor:
  power: FTP; pace: threshold SPEED (so > 1 is faster); HR: LTHR.
Zones are contiguous; each zone is (previous upper, upper].
"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from endurance_planner.models.enums import Sport, TargetKind
from endurance_planner.models.profile import ZoneDef, ZoneModel, ZoneModels
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.units import format_clock


def _model(low_zones: int, *zones: tuple[str, float | None]) -> ZoneModel:
    return ZoneModel(low_zones=low_zones, zones=[ZoneDef(name=n, upper=u) for n, u in zones])


BUILTIN_ZONE_MODELS: dict[str, ZoneModel] = {
    # Coggan power zones (Allen & Coggan, Training and Racing with a Power Meter).
    "coggan_7": _model(
        2,
        ("Z1 Active recovery", 0.55),
        ("Z2 Endurance", 0.75),
        ("Z3 Tempo", 0.90),
        ("Z4 Threshold", 1.05),
        ("Z5 VO2max", 1.20),
        ("Z6 Anaerobic", 1.50),
        ("Z7 Neuromuscular", None),
    ),
    # Daniels-style E/M/T/I/R, made contiguous and expressed relative to T speed.
    # Reference (VDOT 50, T = 4:15/km): M 4:31, I 3:55, R 3:37 fall in their zones.
    "daniels_5": _model(
        1,
        ("E Easy", 0.84),
        ("M Marathon", 0.96),
        ("T Threshold", 1.03),
        ("I Interval", 1.11),
        ("R Repetition", None),
    ),
    # Friel LTHR zones with 5a/5b/5c merged; Z4 tops out at LTHR.
    "lthr_5_bike": _model(
        2,
        ("Z1 Recovery", 0.80),
        ("Z2 Aerobic", 0.89),
        ("Z3 Tempo", 0.93),
        ("Z4 Threshold", 1.00),
        ("Z5 Above threshold", None),
    ),
    "lthr_5_run": _model(
        2,
        ("Z1 Recovery", 0.84),
        ("Z2 Aerobic", 0.89),
        ("Z3 Tempo", 0.94),
        ("Z4 Threshold", 1.00),
        ("Z5 Above threshold", None),
    ),
}

# (fraction of LTHR, intensity factor) anchor points, linearly interpolated.
# Intensity factor = fraction of FTP (bike) or of threshold speed (run).
# Anchors are the matching zone boundaries of the built-in models. Approximate: tune here.
HR_TO_IF: dict[Sport, tuple[tuple[float, float], ...]] = {
    Sport.BIKE: (
        (0.60, 0.40),
        (0.80, 0.55),
        (0.89, 0.75),
        (0.93, 0.90),
        (1.00, 1.00),
        (1.06, 1.20),
    ),
    Sport.RUN: ((0.65, 0.60), (0.84, 0.78), (0.89, 0.84), (0.94, 0.93), (1.00, 1.00), (1.06, 1.11)),
}

# (RPE on a 1-10 scale, intensity factor). Coarse fallback when no threshold is known.
RPE_TO_IF: tuple[tuple[float, float], ...] = (
    (1, 0.40),
    (2, 0.50),
    (3, 0.60),
    (4, 0.70),
    (5, 0.80),
    (6, 0.88),
    (7, 0.95),
    (8, 1.00),
    (9, 1.08),
    (10, 1.20),
)
RPE_LOW_MAX = 4.0  # RPE up to 4 counts as low intensity


def _interp(x: float, points: Sequence[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation, extrapolated with the end segments."""
    segments = list(pairwise(points))
    (x0, y0), (x1, y1) = next((s for s in segments if x <= s[1][0]), segments[-1])
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


def hr_to_if(hr_fraction: float, sport: Sport) -> float:
    return _interp(hr_fraction, HR_TO_IF[sport])


def if_to_hr(intensity_factor: float, sport: Sport) -> float:
    return _interp(intensity_factor, [(i, h) for h, i in HR_TO_IF[sport]])


def rpe_to_if(rpe: float) -> float:
    return _interp(rpe, RPE_TO_IF)


@dataclass(frozen=True)
class ZoneSet:
    """The zone models in use for one athlete."""

    bike_power: ZoneModel
    run_pace: ZoneModel
    bike_hr: ZoneModel
    run_hr: ZoneModel

    def model(self, sport: Sport, kind: TargetKind) -> ZoneModel:
        match kind:
            case TargetKind.POWER:
                return self.bike_power
            case TargetKind.PACE:
                return self.run_pace
            case TargetKind.HR:
                return self.bike_hr if sport is Sport.BIKE else self.run_hr
            case _:
                raise ValueError("RPE targets have no zone model")


def zone_set(config: ZoneModels) -> ZoneSet:
    custom = config.custom
    return ZoneSet(
        bike_power=custom["bike_power"]
        if config.bike_power == "custom"
        else BUILTIN_ZONE_MODELS[config.bike_power],
        run_pace=custom["run_pace"]
        if config.run_pace == "custom"
        else BUILTIN_ZONE_MODELS[config.run_pace],
        bike_hr=custom["hr"] if config.hr == "custom" else BUILTIN_ZONE_MODELS[f"{config.hr}_bike"],
        run_hr=custom["hr"] if config.hr == "custom" else BUILTIN_ZONE_MODELS[f"{config.hr}_run"],
    )


DEFAULT_ZONES = zone_set(ZoneModels())


def low_ceiling(model: ZoneModel) -> float:
    """Upper bound of the low-intensity domain (top of the last 'low' zone)."""
    upper = model.zones[model.low_zones - 1].upper
    assert upper is not None  # guaranteed by ZoneModel validation
    return upper


def zone_number(model: ZoneModel, fraction: float) -> int:
    """1-based zone containing `fraction`."""
    for number, zone in enumerate(model.zones, start=1):
        if zone.upper is None or fraction <= zone.upper + 1e-9:
            return number
    raise AssertionError("unreachable: the top zone is open-ended")


@dataclass(frozen=True)
class ZoneRow:
    """One line of a resolved zone table.

    low/high are absolute values in increasing intensity: W or bpm, or s/km for
    pace (low = slow end). None marks the open end of the bottom/top zone.
    """

    name: str
    low: int | None
    high: int | None
    label: str


def _table(model: ZoneModel, anchor: float, unit: str) -> list[ZoneRow]:
    rows: list[ZoneRow] = []
    previous: int | None = None
    for zone in model.zones:
        low = None if previous is None else previous + 1
        high = None if zone.upper is None else round(zone.upper * anchor)
        if low is None:
            label = f"<= {high} {unit}"
        elif high is None:
            label = f">= {low} {unit}"
        else:
            label = f"{low}-{high} {unit}"
        rows.append(ZoneRow(zone.name, low, high, label))
        previous = high
    return rows


def power_zones(model: ZoneModel, ftp_w: float) -> list[ZoneRow]:
    return _table(model, ftp_w, "W")


def hr_zones(model: ZoneModel, lthr_bpm: float) -> list[ZoneRow]:
    return _table(model, lthr_bpm, "bpm")


def pace_zones(model: ZoneModel, threshold_pace_s: float) -> list[ZoneRow]:
    rows: list[ZoneRow] = []
    slow: int | None = None
    for zone in model.zones:
        fast = None if zone.upper is None else round(threshold_pace_s / zone.upper)
        if slow is None:
            assert fast is not None
            label = f"slower than {format_clock(fast)} /km"
        elif fast is None:
            label = f"faster than {format_clock(slow)} /km"
        else:
            label = f"{format_clock(fast)}-{format_clock(slow)} /km"
        rows.append(ZoneRow(zone.name, slow, fast, label))
        slow = fast
    return rows


def zone_tables(thresholds: ResolvedThresholds, zones: ZoneSet) -> dict[str, list[ZoneRow]]:
    """Every zone table that the known thresholds allow."""
    tables: dict[str, list[ZoneRow]] = {}
    if thresholds.ftp_w is not None:
        tables["Bike power"] = power_zones(zones.bike_power, thresholds.ftp_w)
    if thresholds.bike_lthr_bpm is not None:
        tables["Bike HR"] = hr_zones(zones.bike_hr, thresholds.bike_lthr_bpm)
    if thresholds.threshold_pace_s is not None:
        tables["Run pace"] = pace_zones(zones.run_pace, thresholds.threshold_pace_s)
    if thresholds.run_lthr_bpm is not None:
        tables["Run HR"] = hr_zones(zones.run_hr, thresholds.run_lthr_bpm)
    return tables
