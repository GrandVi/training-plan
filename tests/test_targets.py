import pytest

from builders import session, step, two_by_twenty
from endurance_planner.models.enums import Sport, TargetKind
from endurance_planner.models.plan import Repeat, Step, Target
from endurance_planner.targets import express_target, resolve_session, resolve_target, zone_target
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.zones import BUILTIN_ZONE_MODELS


def _target(kind: TargetKind, low: float, high: float) -> Target:
    return Target(kind=kind, low=low, high=high)


def test_power_target(thresholds: ResolvedThresholds) -> None:
    resolved = resolve_target(_target(TargetKind.POWER, 0.95, 1.00), Sport.BIKE, thresholds)
    assert resolved.power_w == (247, 260)
    assert resolved.hr_bpm == (162, 168)
    assert resolved.label() == "247-260 W, 162-168 bpm"


def test_pace_target(thresholds: ResolvedThresholds) -> None:
    resolved = resolve_target(_target(TargetKind.PACE, 0.98, 1.00), Sport.RUN, thresholds)
    assert resolved.pace_s_per_km == (260, 255)
    assert resolved.label() == "4:15-4:20 /km, 169-172 bpm"


def test_hr_and_rpe_targets(thresholds: ResolvedThresholds) -> None:
    hr = resolve_target(_target(TargetKind.HR, 0.81, 0.89), Sport.BIKE, thresholds)
    assert (hr.power_w, hr.hr_bpm) == (None, (136, 150))
    rpe = resolve_target(_target(TargetKind.RPE, 4, 4), Sport.BIKE, thresholds)
    assert rpe.label() == "150 bpm"  # RPE 4 -> IF 0.75 -> 89% LTHR


def test_no_hr_guide_above_threshold(thresholds: ResolvedThresholds) -> None:
    resolved = resolve_target(_target(TargetKind.POWER, 1.10, 1.20), Sport.BIKE, thresholds)
    assert resolved.label() == "286-312 W"


def test_unknown_threshold_gives_empty_resolution() -> None:
    resolved = resolve_target(_target(TargetKind.POWER, 0.9, 1.0), Sport.BIKE, ResolvedThresholds())
    assert resolved.label() == ""


def test_retarget_keeps_structure_and_load(thresholds: ResolvedThresholds) -> None:
    before = resolve_session(two_by_twenty(), thresholds)
    after = resolve_session(before, thresholds.model_copy(update={"ftp_w": 280.0}))
    work_before, work_after = list(before.flat_steps())[1], list(after.flat_steps())[1]
    assert work_before.resolved is not None and work_after.resolved is not None
    assert work_before.resolved.power_w == (247, 260)
    assert work_after.resolved.power_w == (266, 280)
    assert work_after.target == work_before.target
    assert (after.tss, after.duration_s) == (before.tss, before.duration_s)
    assert isinstance(after.steps[1], Repeat) and after.steps[1].count == 2
    assert all(s.resolved is not None for s in after.flat_steps())


def test_distance_step_duration_follows_threshold_pace(thresholds: ResolvedThresholds) -> None:
    km_rep = step(240, 1.04, 1.06, kind=TargetKind.PACE, distance_m=1000)
    resolved = resolve_session(session([km_rep], Sport.RUN), thresholds)
    first = resolved.steps[0]
    assert isinstance(first, Step)
    assert first.duration_s == 243  # 1 km at 255 / 1.05 s/km
    assert first.distance_m == 1000


def test_zone_target() -> None:
    coggan = BUILTIN_ZONE_MODELS["coggan_7"]
    z2 = zone_target(coggan, TargetKind.POWER, 2)
    assert (z2.low, z2.high) == (0.55, 0.75)
    z1 = zone_target(coggan, TargetKind.POWER, 1)
    assert (z1.low, z1.high) == (0.44, 0.55)
    z7 = zone_target(coggan, TargetKind.POWER, 7)
    assert (z7.low, z7.high) == (1.5, 1.65)


@pytest.mark.parametrize(("low", "high"), [(1.0, 0.9), (0.1, 0.5), (0.5, 3.5)])
def test_invalid_target_range(low: float, high: float) -> None:
    with pytest.raises(ValueError, match="low <= high"):
        _target(TargetKind.POWER, low, high)


def test_express_target_in_athlete_mode() -> None:
    easy = _target(TargetKind.POWER, 0.60, 0.72)
    assert express_target(easy, Sport.BIKE, "power") is easy
    as_hr = express_target(easy, Sport.BIKE, "hr")
    assert (as_hr.kind, as_hr.low, as_hr.high) == (TargetKind.HR, 0.82, 0.88)
    as_rpe = express_target(easy, Sport.BIKE, "rpe")
    assert (as_rpe.kind, as_rpe.low, as_rpe.high) == (TargetKind.RPE, 2.5, 3.5)
    # HR is not usable for short efforts above threshold: falls back to RPE.
    vo2 = express_target(_target(TargetKind.POWER, 1.08, 1.15), Sport.BIKE, "hr")
    assert vo2.kind is TargetKind.RPE
    # Targets that are not in the sport's native unit are left alone.
    hills = _target(TargetKind.RPE, 8, 9)
    assert express_target(hills, Sport.RUN, "hr") is hills
    assert _target(TargetKind.PACE, 0.98, 1.0).label() == "98-100% threshold pace"
    assert hills.label() == "RPE 8-9"
