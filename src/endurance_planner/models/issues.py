"""Structured error/warning, shared by profile checks and plan validation."""

from pydantic import BaseModel

from endurance_planner.models.enums import Severity


class Issue(BaseModel):
    severity: Severity
    code: str
    message: str
    week: int | None = None

    def __str__(self) -> str:
        where = f" (week {self.week})" if self.week is not None else ""
        return f"[{self.severity.value}] {self.code}{where}: {self.message}"
