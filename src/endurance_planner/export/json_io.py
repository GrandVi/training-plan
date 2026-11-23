"""Adapter: plan and history files (the JSON forms themselves are in serialization.py)."""

from pathlib import Path

from endurance_planner.files import write_text_atomic
from endurance_planner.models.history import History
from endurance_planner.models.plan import Plan
from endurance_planner.serialization import dump_history, dump_plan, parse_history, parse_plan

__all__ = ["dump_plan", "load_history", "load_plan", "save_history", "save_plan"]


def save_plan(plan: Plan, path: Path) -> None:
    """Write the plan atomically (a reader never sees a half-written file)."""
    write_text_atomic(path, dump_plan(plan))


def load_plan(path: Path) -> Plan:
    return parse_plan(path.read_text(encoding="utf-8"))


def save_history(history: History, path: Path) -> None:
    write_text_atomic(path, dump_history(history))


def load_history(path: Path) -> History:
    """history.json, or an empty history when the file does not exist yet."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return History()
    return parse_history(text)
