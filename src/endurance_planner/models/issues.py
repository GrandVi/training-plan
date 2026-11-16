"""Structured error/warning, shared by profile checks and plan validation."""

from pydantic import BaseModel

from endurance_planner.models.enums import Severity


class Issue(BaseModel):
    severity: Severity
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.code}: {self.message}"
