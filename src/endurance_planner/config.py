"""Adapter: profile loading from a file (parsing is in serialization.py)."""

from pathlib import Path

from endurance_planner.models.profile import Profile
from endurance_planner.serialization import ProfileError, parse_profile

__all__ = ["ProfileError", "load_profile"]


def load_profile(path: Path) -> Profile:
    return parse_profile(path.read_text(encoding="utf-8"), str(path))
