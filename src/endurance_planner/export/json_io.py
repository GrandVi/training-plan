"""Adapter: plan files (the JSON form itself is in serialization.py)."""

from pathlib import Path

from endurance_planner.files import write_text_atomic
from endurance_planner.models.plan import Plan
from endurance_planner.serialization import dump_plan, parse_plan

__all__ = ["dump_plan", "load_plan", "save_plan"]


def save_plan(plan: Plan, path: Path) -> None:
    """Write the plan atomically (a reader never sees a half-written file)."""
    write_text_atomic(path, dump_plan(plan))


def load_plan(path: Path) -> Plan:
    return parse_plan(path.read_text(encoding="utf-8"))
