"""Text forms of the profile (YAML), the plan and the history (JSON). Pure: no file access.

File adapters (config.py, export/json_io.py, storage.py) read and write the text.
"""

import yaml
from pydantic import ValidationError

from endurance_planner.models.history import HISTORY_SCHEMA_VERSION, History
from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile


class ProfileError(ValueError):
    """profile.yaml is unreadable or invalid; the message lists every problem."""


def parse_profile(text: str, source: str = "profile.yaml") -> Profile:
    """Profile from YAML text; `source` names it in error messages."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{source}: expected a YAML mapping at top level")
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(part) for part in err['loc']) or '<profile>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ProfileError(f"{source}: invalid profile\n" + "\n".join(lines)) from exc


def dump_plan(plan: Plan) -> str:
    """Canonical JSON form of a plan."""
    return plan.model_dump_json(indent=2) + "\n"


def parse_plan(text: str) -> Plan:
    """Plan from its JSON form (older schema versions are migrated in memory)."""
    return Plan.model_validate_json(text)


def dump_history(history: History) -> str:
    """Canonical JSON form of history.json."""
    return history.model_dump_json(indent=2) + "\n"


def parse_history(text: str) -> History:
    history = History.model_validate_json(text)
    if history.schema_version > HISTORY_SCHEMA_VERSION:
        raise ValueError(
            f"history schema_version {history.schema_version} is not supported (this version "
            f"reads up to {HISTORY_SCHEMA_VERSION}); update endurance-planner"
        )
    return history
