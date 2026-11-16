import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from builders import EXAMPLE
from endurance_planner.config import ProfileError, load_profile
from endurance_planner.models.enums import GoalType, Sport, Weekday
from endurance_planner.models.profile import Profile

DELETE = object()


def _edit(data: dict[str, Any], path: str, value: object) -> None:
    *parents, leaf = path.split(".")
    node = data
    for key in parents:
        node = node[key]
    if value is DELETE:
        del node[leaf]
    else:
        node[leaf] = value


def _load(tmp_path: Path, data: dict[str, Any]) -> Profile:
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_profile(path)


def test_example_profile_loads() -> None:
    profile = load_profile(EXAMPLE)
    assert profile.athlete.name == "Victor"
    assert profile.thresholds.run is not None
    assert profile.thresholds.run.threshold_pace == 255
    assert profile.thresholds.run.recent_race is not None
    assert profile.thresholds.run.recent_race.time == 2670
    assert profile.goals[0].target.time == 41 * 60
    assert profile.goals[0].date == dt.date(2027, 3, 14)
    week = profile.availability.weekdays
    assert (week[Weekday.MON].bike, week[Weekday.MON].run) == (0, 0)
    assert week[Weekday.SAT].bike == 240
    assert profile.availability.training_days() == [
        Weekday.TUE,
        Weekday.WED,
        Weekday.THU,
        Weekday.SAT,
        Weekday.SUN,
    ]
    assert profile.preferences.limits.max_weekly_ramp == 0.08


def test_defaults_and_missing_days_are_rest(profile_data: dict[str, Any]) -> None:
    del profile_data["preferences"]
    del profile_data["availability"]["weekdays"]["mon"]
    profile = Profile.model_validate(profile_data)
    assert profile.preferences.intensity_ratio.low == 0.80
    assert profile.preferences.mesocycle_pattern == "3:1"
    assert profile.availability.weekdays[Weekday.MON].bike == 0


def test_goals_sorted_and_goal_sport(profile_data: dict[str, Any]) -> None:
    profile_data["goals"].reverse()
    profile_data["goals"].append(
        {"name": "Ultra", "date": dt.date(2027, 9, 1), "type": "ultra_endurance"}
    )
    profile = Profile.model_validate(profile_data)
    assert [g.type for g in profile.goals] == [
        GoalType.RUN_10K,
        GoalType.MTB_MARATHON,
        GoalType.ULTRA_ENDURANCE,
    ]
    assert [profile.goal_sport(g) for g in profile.goals] == [Sport.RUN, Sport.BIKE, Sport.BIKE]


def test_unquoted_yaml_pace_is_seconds(profile_data: dict[str, Any]) -> None:
    profile_data["thresholds"]["run"]["threshold_pace"] = yaml.safe_load("4:15")
    profile = Profile.model_validate(profile_data)
    assert profile.thresholds.run is not None
    assert profile.thresholds.run.threshold_pace == 255


def test_clock_round_trip(profile: Profile) -> None:
    dumped = profile.model_dump(mode="json")
    assert dumped["thresholds"]["run"]["threshold_pace"] == "4:15"
    assert Profile.model_validate(dumped) == profile


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        # missing required fields
        ("athlete.name", DELETE, "athlete.name"),
        ("availability.days_per_week", DELETE, "availability.days_per_week"),
        ("current_fitness.weekly_hours", DELETE, "current_fitness.weekly_hours"),
        ("goals", [], "goals"),
        # inconsistent values
        ("preferences.intensity_ratio", {"low": 0.8, "high": 0.3}, "sum to 1"),
        ("availability.long_session_day", "mon", "rest day"),
        ("availability.days_per_week", 6, "only 5 weekdays"),
        ("availability.indoor_trainer_days", ["mon"], "no bike time"),
        ("thresholds.run", DELETE, "thresholds.run has no anchor"),
        ("thresholds.bike.ftp_w", DELETE, "thresholds.bike has no anchor"),
        ("thresholds.zone_models.bike_power", "custom", "custom.bike_power is missing"),
        ("thresholds.run.threshold_pace", "fast", "m:ss"),
        ("thresholds.bike.lthr_bpm", 20, "greater than 60"),
        # typo in a key
        ("athlete.wieght_kg", 72, "athlete.wieght_kg"),
    ],
)
def test_invalid_profile(
    tmp_path: Path, profile_data: dict[str, Any], path: str, value: object, expected: str
) -> None:
    _edit(profile_data, path, value)
    with pytest.raises(ProfileError, match=expected):
        _load(tmp_path, profile_data)


def test_goal_sport_mismatch(profile_data: dict[str, Any]) -> None:
    profile_data["goals"][0]["sport"] = "bike"
    with pytest.raises(ValueError, match="is a run goal"):
        Profile.model_validate(profile_data)


def test_hr_mode_only_needs_lthr(profile_data: dict[str, Any]) -> None:
    del profile_data["thresholds"]["bike"]["ftp_w"]
    profile_data["thresholds"]["target_mode"]["bike"] = "hr"
    assert Profile.model_validate(profile_data).thresholds.anchored(Sport.BIKE)


def test_custom_zone_model_validation(profile_data: dict[str, Any]) -> None:
    zone_models = profile_data["thresholds"]["zone_models"]
    zone_models["bike_power"] = "custom"
    zone_models["custom"] = {
        "bike_power": {
            "low_zones": 1,
            "zones": [
                {"name": "low", "upper": 0.75},
                {"name": "mid", "upper": 1.0},
                {"name": "hi"},
            ],
        }
    }
    Profile.model_validate(profile_data)
    zone_models["custom"]["bike_power"]["zones"][1]["upper"] = 0.5
    with pytest.raises(ValueError, match="strictly increasing"):
        Profile.model_validate(profile_data)


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("athlete: [unclosed", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid YAML"):
        load_profile(path)
