"""JSON Schemas of profile.yaml and plan.json."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from builders import EXAMPLE, TODAY, example_data, run_10k_profile
from endurance_planner.cli import app
from endurance_planner.models.plan import Plan
from endurance_planner.planner import generate_plan
from endurance_planner.profile_tools import dump_profile, starter_profile
from endurance_planner.schemas import (
    PROFILE_SCHEMA_URL,
    plan_json_schema,
    profile_json_schema,
)

SCHEMAS = Path(__file__).parent.parent / "schemas"
runner = CliRunner()


def _as_json(data: Any) -> Any:
    """YAML data as a JSON document (dates become ISO strings, as an editor sees them)."""
    return json.loads(json.dumps(data, default=str))


def _errors(schema: dict[str, Any], data: Any) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    return [error.message for error in validator.iter_errors(_as_json(data))]


@pytest.mark.parametrize(
    ("name", "build"), [("profile", profile_json_schema), ("plan", plan_json_schema)]
)
def test_committed_schemas_are_up_to_date(name: str, build: Any) -> None:
    """Regenerate with: endurance-planner {profile,plan} schema -o schemas/<name>.schema.json"""
    stored = (SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8")
    assert stored == json.dumps(build(), indent=2) + "\n"
    jsonschema.Draft202012Validator.check_schema(build())


def test_example_profile_matches_the_schema() -> None:
    assert _errors(profile_json_schema(), example_data()) == []


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("athlete", "nmae"), "x", "'nmae' was unexpected"),
        (("thresholds", "run", "threshold_pace"), "fast", "is not valid under any"),
        (("availability", "weekdays", "mon"), "off", "is not valid"),
        (("availability", "days_per_week"), 9, "maximum"),
    ],
)
def test_schema_reports_mistakes(path: tuple[str, ...], value: object, expected: str) -> None:
    data = example_data()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert any(expected in message for message in _errors(profile_json_schema(), data))


def test_plan_file_matches_the_schema() -> None:
    plan: Plan = generate_plan(run_10k_profile(), TODAY)
    assert _errors(plan_json_schema(), plan.model_dump(mode="json")) == []


def test_profiles_carry_the_language_server_header() -> None:
    first = EXAMPLE.read_text(encoding="utf-8").splitlines()[0]
    assert first == "# yaml-language-server: $schema=../schemas/profile.schema.json"
    assert (EXAMPLE.parent / first.split("=", 1)[1]).resolve() == (
        SCHEMAS / "profile.schema.json"
    ).resolve()
    data = starter_profile(
        name="Ana",
        ftp_w=250,
        threshold_pace="4:30",
        bike_hours=5,
        run_hours=2,
        days_per_week=4,
        weekly_hours_budget=7,
        goal_name="10k",
        goal_date=TODAY.replace(year=2027),
        goal_type="run_10k",
    )
    text = dump_profile(data)
    assert text.splitlines()[0] == f"# yaml-language-server: $schema={PROFILE_SCHEMA_URL}"
    assert _errors(profile_json_schema(), yaml.safe_load(text)) == []


@pytest.mark.parametrize("command", ["profile", "plan"])
def test_schema_commands(command: str, tmp_path: Path) -> None:
    printed = runner.invoke(app, [command, "schema"])
    assert printed.exit_code == 0, printed.output
    assert json.loads(printed.output)["$schema"].startswith("https://json-schema.org/")
    out = tmp_path / "schema.json"
    written = runner.invoke(app, [command, "schema", "-o", str(out)])
    assert written.exit_code == 0
    assert out.read_text(encoding="utf-8") == printed.output
