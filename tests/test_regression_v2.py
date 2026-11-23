"""Plans without strength, history or new goal types stay byte-identical (F10, F7, F8, F9).

tests/fixtures was generated with the code of the Portability milestone (before F10):
- example_plan_v2.json: the example profile's plan, exactly as `plan generate` writes it;
- property_plan_digests_v2.json: SHA-256 of each property-test plan file (seeds 0-149).
New fields are omitted while they hold their default, so no field is left out here.
"""

import hashlib
import json
from pathlib import Path

import pytest

from builders import EXAMPLE, TODAY
from endurance_planner.config import load_profile
from endurance_planner.planner import generate_plan
from endurance_planner.serialization import dump_plan
from test_property import random_profile

FIXTURES = Path(__file__).parent / "fixtures"
DIGESTS: dict[str, str] = json.loads((FIXTURES / "property_plan_digests_v2.json").read_text())


def test_example_plan_file_is_unchanged() -> None:
    plan = generate_plan(load_profile(EXAMPLE), TODAY)
    stored = (FIXTURES / "example_plan_v2.json").read_text(encoding="utf-8")
    assert dump_plan(plan) == stored


@pytest.mark.parametrize("seed", range(150))
def test_property_plan_files_are_unchanged(seed: int) -> None:
    text = dump_plan(generate_plan(random_profile(seed), TODAY))
    assert hashlib.sha256(text.encode()).hexdigest() == DIGESTS[str(seed)]
