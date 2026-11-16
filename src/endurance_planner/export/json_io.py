"""Canonical JSON form of a plan."""

from pathlib import Path

from endurance_planner.models.plan import Plan


def dump_plan(plan: Plan) -> str:
    return plan.model_dump_json(indent=2) + "\n"


def save_plan(plan: Plan, path: Path) -> None:
    path.write_text(dump_plan(plan), encoding="utf-8")


def load_plan(path: Path) -> Plan:
    return Plan.model_validate_json(path.read_text(encoding="utf-8"))
