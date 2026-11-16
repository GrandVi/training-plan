from typing import Any

import pytest

from endurance_planner.models.enums import Sport, TargetKind
from endurance_planner.models.profile import Profile
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.zones import (
    BUILTIN_ZONE_MODELS,
    DEFAULT_ZONES,
    hr_to_if,
    hr_zones,
    if_to_hr,
    if_to_rpe,
    low_ceiling,
    pace_zones,
    power_zones,
    rpe_to_if,
    zone_number,
    zone_set,
    zone_tables,
)

COGGAN = BUILTIN_ZONE_MODELS["coggan_7"]
DANIELS = BUILTIN_ZONE_MODELS["daniels_5"]


@pytest.mark.parametrize(
    ("ftp", "uppers"),
    [
        # Coggan: 55 / 75 / 90 / 105 / 120 / 150 % of FTP
        (260, [143, 195, 234, 273, 312, 390, None]),
        (200, [110, 150, 180, 210, 240, 300, None]),
    ],
)
def test_coggan_power_zones(ftp: int, uppers: list[int | None]) -> None:
    rows = power_zones(COGGAN, ftp)
    assert [row.high for row in rows] == uppers
    assert [row.low for row in rows] == [None] + [u + 1 for u in uppers[:-1] if u is not None]


def test_power_zone_labels() -> None:
    labels = [row.label for row in power_zones(COGGAN, 260)]
    assert labels[0] == "<= 143 W"
    assert labels[3] == "235-273 W"
    assert labels[6] == ">= 391 W"


def test_daniels_pace_zones() -> None:
    rows = pace_zones(DANIELS, 255)  # T = 4:15 /km
    assert [row.label for row in rows] == [
        "slower than 5:04 /km",
        "4:26-5:04 /km",
        "4:08-4:26 /km",
        "3:50-4:08 /km",
        "faster than 3:50 /km",
    ]


@pytest.mark.parametrize(
    ("pace_s", "zone"),
    [
        (320, 1),  # E 5:20
        (271, 2),  # M 4:31
        (255, 3),  # T 4:15
        (235, 4),  # I 3:55
        (217.5, 5),  # R: 400 m in 87 s
    ],
)
def test_daniels_vdot50_reference_paces(pace_s: float, zone: int) -> None:
    """Daniels' VDOT 50 training paces fall in the matching zone for T = 4:15 /km."""
    assert zone_number(DANIELS, 255 / pace_s) == zone


def test_lthr_zones() -> None:
    # Friel: bike 81 / 90 / 94 / 100 %, run 85 / 90 / 95 / 100 % of LTHR as zone starts.
    bike = hr_zones(BUILTIN_ZONE_MODELS["lthr_5_bike"], 168)
    assert [row.high for row in bike] == [134, 150, 156, 168, None]
    run = hr_zones(BUILTIN_ZONE_MODELS["lthr_5_run"], 172)
    assert [row.high for row in run] == [144, 153, 162, 172, None]
    assert bike[4].label == ">= 169 bpm"


def test_zone_number_boundaries() -> None:
    assert zone_number(COGGAN, 0.75) == 2
    assert zone_number(COGGAN, 0.76) == 3
    assert zone_number(COGGAN, 1.05) == 4
    assert zone_number(COGGAN, 2.0) == 7


def test_low_ceiling() -> None:
    assert low_ceiling(COGGAN) == 0.75
    assert low_ceiling(DANIELS) == 0.84
    assert low_ceiling(DEFAULT_ZONES.model(Sport.RUN, TargetKind.HR)) == 0.89


def test_hr_and_rpe_mappings() -> None:
    for sport in Sport:
        assert hr_to_if(1.0, sport) == pytest.approx(1.0)
        for hr_fraction in (0.5, 0.7, 0.85, 0.97, 1.1):
            assert if_to_hr(hr_to_if(hr_fraction, sport), sport) == pytest.approx(hr_fraction)
    assert hr_to_if(0.89, Sport.BIKE) == pytest.approx(0.75)  # top of Z2 on both scales
    assert hr_to_if(0.89, Sport.RUN) == pytest.approx(0.84)
    assert rpe_to_if(4, Sport.BIKE) == pytest.approx(0.75)  # RPE 4 = top of the low zones
    assert rpe_to_if(4, Sport.RUN) == pytest.approx(0.84)
    assert rpe_to_if(7.5, Sport.BIKE) == pytest.approx(1.0)
    for sport in Sport:
        for rpe in (1, 3.5, 6, 10):
            assert if_to_rpe(rpe_to_if(rpe, sport), sport) == pytest.approx(rpe)
    assert if_to_rpe(3.0, Sport.BIKE) == 10


def test_zone_tables_follow_available_thresholds(thresholds: ResolvedThresholds) -> None:
    assert list(zone_tables(thresholds, DEFAULT_ZONES)) == [
        "Bike power",
        "Bike HR",
        "Run pace",
        "Run HR",
    ]
    assert list(zone_tables(ResolvedThresholds(ftp_w=250), DEFAULT_ZONES)) == ["Bike power"]


def test_custom_zone_set(profile_data: dict[str, Any]) -> None:
    zone_models = profile_data["thresholds"]["zone_models"]
    zone_models["hr"] = "custom"
    zone_models["custom"] = {
        "hr": {
            "low_zones": 1,
            "zones": [
                {"name": "low", "upper": 0.85},
                {"name": "mid", "upper": 1.0},
                {"name": "hi"},
            ],
        }
    }
    zones = zone_set(Profile.model_validate(profile_data).thresholds.zone_models)
    assert zones.bike_hr is zones.run_hr
    assert low_ceiling(zones.bike_hr) == 0.85
    assert zones.bike_power is COGGAN
