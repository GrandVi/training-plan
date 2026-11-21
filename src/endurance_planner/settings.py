"""Adapter: settings read from the process environment and an optional .env file.

Secrets (the intervals.icu API key) are read here, at the edge, and handed to the core as
plain arguments; the core never reads the environment or files.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass


def load_env_file(path: str = ".env") -> dict[str, str]:
    """KEY=VALUE lines of a .env file (missing file = empty)."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return values


def environment(env_file: str | None = ".env") -> dict[str, str]:
    """The .env file overridden by the process environment."""
    return {**(load_env_file(env_file) if env_file else {}), **os.environ}


@dataclass(frozen=True)
class IntervalsSettings:
    api_key: str | None
    athlete_id: str


def intervals_settings(env: Mapping[str, str], athlete_id: str | None = None) -> IntervalsSettings:
    """intervals.icu credentials: INTERVALS_API_KEY, INTERVALS_ATHLETE_ID (default 0)."""
    return IntervalsSettings(
        api_key=env.get("INTERVALS_API_KEY") or None,
        athlete_id=athlete_id or env.get("INTERVALS_ATHLETE_ID", "0"),
    )
