from typing import Any

import pytest
import yaml

from builders import EXAMPLE, TODAY
from endurance_planner.models.profile import Profile
from endurance_planner.thresholds import ResolvedThresholds, resolve_thresholds


@pytest.fixture
def profile_data() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture
def profile(profile_data: dict[str, Any]) -> Profile:
    return Profile.model_validate(profile_data)


@pytest.fixture
def thresholds(profile: Profile) -> ResolvedThresholds:
    return resolve_thresholds(profile, TODAY)[0]
