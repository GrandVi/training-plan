"""Plans made before schema v2 stay identical, and stored v1 plans still load.

tests/fixtures was generated with the code of milestone 6 (before any M7 change):
- example_plan_v1.json: the example profile's plan, a stored schema v1 file;
- property_plan_digests.json: SHA-256 of each property-test plan (seeds 0-149).
Fields added by schema v2 are left out of the comparison.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from builders import EXAMPLE, TODAY
from endurance_planner.config import load_profile
from endurance_planner.export.json_io import dump_plan, load_plan
from endurance_planner.models.plan import PLAN_SCHEMA_VERSION, Plan
from endurance_planner.planner import generate_plan
from test_property import random_profile

FIXTURES = Path(__file__).parent / "fixtures"
V1_PLAN = FIXTURES / "example_plan_v1.json"
NEW_FIELDS = {"schema_version", "amendments", "pinned", "rung", "amendment_id"}
DIGESTS: dict[str, str] = json.loads((FIXTURES / "property_plan_digests.json").read_text())


def strip(node: Any) -> Any:
    """JSON data without the fields added after schema v1."""
    if isinstance(node, dict):
        return {key: strip(value) for key, value in node.items() if key not in NEW_FIELDS}
    if isinstance(node, list):
        return [strip(value) for value in node]
    return node


def digest(plan: Plan) -> str:
    text = json.dumps(strip(plan.model_dump(mode="json")), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def test_example_plan_is_unchanged() -> None:
    plan = generate_plan(load_profile(EXAMPLE), TODAY)
    stored = json.loads(V1_PLAN.read_text(encoding="utf-8"))
    assert strip(plan.model_dump(mode="json")) == strip(stored)


@pytest.mark.parametrize("seed", range(150))
def test_property_plans_are_unchanged(seed: int) -> None:
    assert digest(generate_plan(random_profile(seed), TODAY)) == DIGESTS[str(seed)]


def test_v1_plan_is_migrated_in_memory(tmp_path: Path) -> None:
    assert json.loads(V1_PLAN.read_text(encoding="utf-8"))["schema_version"] == 1
    plan = load_plan(V1_PLAN)
    assert plan.schema_version == PLAN_SCHEMA_VERSION == 2
    assert plan.amendments == []
    sessions = [s for week in plan.weeks for s in week.sessions]
    assert sessions and all(not s.pinned and s.amendment_id is None for s in sessions)
    assert all(s.rung is None for s in sessions)
    saved = tmp_path / "plan.json"
    saved.write_text(dump_plan(plan), encoding="utf-8")
    assert load_plan(saved) == plan  # written back as v2
    assert json.loads(saved.read_text(encoding="utf-8"))["schema_version"] == 2


@pytest.mark.parametrize("version", [0, 3, "2"])
def test_unknown_schema_version_is_refused(tmp_path: Path, version: object) -> None:
    data = json.loads(V1_PLAN.read_text(encoding="utf-8"))
    data["schema_version"] = version
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version .* is not supported"):
        load_plan(path)
