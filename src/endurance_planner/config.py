"""Profile loading."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from endurance_planner.models.profile import Profile


class ProfileError(ValueError):
    """profile.yaml is unreadable or invalid; the message lists every problem."""


def load_profile(path: Path) -> Profile:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: expected a YAML mapping at top level")
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(part) for part in err['loc']) or '<profile>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ProfileError(f"{path}: invalid profile\n" + "\n".join(lines)) from exc
