from typing import Any

import pytest

from builders import TODAY, example_data
from endurance_planner.models.profile import Profile
from endurance_planner.thresholds import ResolvedThresholds, resolve_thresholds


@pytest.fixture
def profile_data() -> dict[str, Any]:
    return example_data()


@pytest.fixture
def profile(profile_data: dict[str, Any]) -> Profile:
    return Profile.model_validate(profile_data)


@pytest.fixture
def thresholds(profile: Profile) -> ResolvedThresholds:
    return resolve_thresholds(profile, TODAY)[0]
