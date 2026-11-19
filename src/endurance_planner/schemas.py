"""JSON Schemas of profile.yaml and plan.json, for editors and other tools.

The committed copies in schemas/ are kept up to date by a test. Editors with the YAML
language server validate and complete profile.yaml through its header line:
    # yaml-language-server: $schema=<path or URL of profile.schema.json>
"""

from typing import Any

from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile

DRAFT = "https://json-schema.org/draft/2020-12/schema"
REPO_RAW = "https://raw.githubusercontent.com/GrandVi/training-plan/main"
PROFILE_SCHEMA_URL = f"{REPO_RAW}/schemas/profile.schema.json"
PLAN_SCHEMA_URL = f"{REPO_RAW}/schemas/plan.schema.json"


def schema_header(url: str = PROFILE_SCHEMA_URL) -> str:
    """First line of a profile.yaml, read by the YAML language server."""
    return f"# yaml-language-server: $schema={url}\n"


def profile_json_schema() -> dict[str, Any]:
    """Schema of profile.yaml as it is written by hand (validation mode)."""
    schema = Profile.model_json_schema(mode="validation")
    return {"$schema": DRAFT, "$id": PROFILE_SCHEMA_URL, **schema}


def plan_json_schema() -> dict[str, Any]:
    """Schema of plan.json as it is written by the tool (serialization mode)."""
    schema = Plan.model_json_schema(mode="serialization")
    return {"$schema": DRAFT, "$id": PLAN_SCHEMA_URL, **schema}
