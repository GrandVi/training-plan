import datetime as dt
from typing import Any

import pytest

from builders import TODAY
from endurance_planner.models.profile import Profile
from endurance_planner.thresholds import (
    ftp_from_20min,
    max_hr_from_age,
    resolve_thresholds,
    threshold_pace_from_race,
)


def _codes(data: dict[str, Any], today: dt.date = TODAY) -> set[str]:
    return {issue.code for issue in resolve_thresholds(Profile.model_validate(data), today)[1]}


def test_estimators() -> None:
    assert ftp_from_20min(280) == pytest.approx(266)
    # A 60-min race gives its own pace.
    assert threshold_pace_from_race(15, 3600) == pytest.approx(240)
    # 10 km in 44:30 -> ~4:32 /km (VDOT 46 tables give T = 4:33 /km).
    assert threshold_pace_from_race(10, 2670) == pytest.approx(271.6, abs=0.5)
    # Shorter races are run faster than threshold, longer ones slower.
    assert threshold_pace_from_race(5, 1200) > 240
    assert threshold_pace_from_race(21.1, 5400) < 5400 / 21.1
    assert max_hr_from_age(36) == 183


def test_example_resolves(profile: Profile) -> None:
    resolved, issues = resolve_thresholds(profile, TODAY)
    # The example's 10 km in 44:30 implies ~4:32 /km, 6.5% off the stated 4:15 /km.
    assert [i.code for i in issues] == ["pace_race_mismatch"]
    assert resolved.estimated == ()
    assert resolved.ftp_w == 260
    assert resolved.threshold_pace_s == 255
    assert (resolved.bike_lthr_bpm, resolved.run_lthr_bpm) == (168, 172)


def test_ftp_estimated_from_20min_test(profile_data: dict[str, Any]) -> None:
    bike = profile_data["thresholds"]["bike"]
    del bike["ftp_w"]
    bike["ftp_20min_w"] = 280
    resolved, issues = resolve_thresholds(Profile.model_validate(profile_data), TODAY)
    assert resolved.ftp_w == pytest.approx(266)
    assert "ftp_w" in resolved.estimated
    assert "estimated_ftp" in [i.code for i in issues]


def test_threshold_pace_estimated_from_race(profile_data: dict[str, Any]) -> None:
    del profile_data["thresholds"]["run"]["threshold_pace"]
    resolved, issues = resolve_thresholds(Profile.model_validate(profile_data), TODAY)
    assert resolved.threshold_pace_s == pytest.approx(271.6, abs=0.5)
    assert "threshold_pace_s" in resolved.estimated
    assert [i.code for i in issues] == ["estimated_threshold_pace"]


def test_max_hr_estimated_from_age(profile_data: dict[str, Any]) -> None:
    del profile_data["thresholds"]["bike"]["max_hr_bpm"]
    resolved, issues = resolve_thresholds(Profile.model_validate(profile_data), TODAY)
    assert resolved.bike_max_hr_bpm == 183
    assert "bike_max_hr_bpm" in resolved.estimated
    assert "estimated_max_hr" in [i.code for i in issues]


def test_stale_test_warning(profile_data: dict[str, Any]) -> None:
    assert "stale_test" not in _codes(profile_data, dt.date(2026, 11, 30))  # exactly 8 weeks
    assert "stale_test" in _codes(profile_data, dt.date(2026, 12, 1))
    assert "future_test_date" in _codes(profile_data, dt.date(2026, 10, 1))


def test_inconsistent_thresholds_warn(profile_data: dict[str, Any]) -> None:
    profile_data["thresholds"]["bike"]["lthr_bpm"] = 190  # above max HR 188
    profile_data["thresholds"]["run"]["lthr_bpm"] = 120  # 63% of max HR
    profile_data["thresholds"]["bike"]["ftp_w"] = 500  # 6.9 W/kg
    profile_data["thresholds"]["run"]["threshold_pace"] = "3:40"  # race says ~4:32
    assert _codes(profile_data) == {
        "lthr_above_max_hr",
        "lthr_implausible",
        "ftp_implausible",
        "pace_race_mismatch",
    }
