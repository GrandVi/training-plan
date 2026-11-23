"""Adapter: the profile and the plans in a data directory (services.Storage on files).

    <data_dir>/profile.yaml       hand-edited; read at every request, never written by the
                                  services (save_profile refuses to replace an existing file)
    <data_dir>/plans/<id>.json    one file per plan
    <data_dir>/current            id of the current plan
    <data_dir>/history.json       completed activities and wellness (written by sync)
    <data_dir>/.lock              lock file: one writer at a time

Every write goes to a temporary file in the same folder that is then renamed over the
target, so a crash leaves the old file or the new one, never a half-written one.
"""

import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import yaml

from endurance_planner.files import write_text_atomic
from endurance_planner.models.history import History
from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile
from endurance_planner.schemas import schema_header
from endurance_planner.serialization import (
    ProfileError,
    dump_history,
    dump_plan,
    parse_history,
    parse_plan,
    parse_profile,
)
from endurance_planner.services import PlanNotFound, StorageBusy

PLAN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05


def _try_lock(handle: IO[str]) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileStorage:
    """services.Storage in a data directory (see the module docstring)."""

    def __init__(self, data_dir: Path, *, lock_timeout_s: float = LOCK_TIMEOUT_S) -> None:
        self.data_dir = Path(data_dir)
        self.lock_timeout_s = lock_timeout_s

    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.yaml"

    @property
    def plans_dir(self) -> Path:
        return self.data_dir / "plans"

    # ------------------------------------------------------------------ profile
    def load_profile(self) -> Profile:
        try:
            text = self.profile_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ProfileError("no profile.yaml in the data directory") from exc
        return parse_profile(text, "profile.yaml")  # messages name no server path

    def save_profile(self, profile: Profile, *, overwrite: bool = False) -> None:
        """Write a profile (setup only). A hand-edited profile.yaml is never replaced unless
        `overwrite`; the services never call this."""
        if self.profile_path.exists() and not overwrite:
            raise FileExistsError(f"{self.profile_path} exists and is edited by hand")
        data = profile.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        text = schema_header() + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        write_text_atomic(self.profile_path, text)

    # ------------------------------------------------------------------ plans
    def _plan_path(self, plan_id: str) -> Path:
        if not PLAN_ID.match(plan_id):
            raise PlanNotFound(f"invalid plan id {plan_id!r}")
        return self.plans_dir / f"{plan_id}.json"

    def current_plan_id(self) -> str | None:
        try:
            return (self.data_dir / "current").read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    def load_plan(self, plan_id: str | None = None) -> Plan:
        plan_id = plan_id or self.current_plan_id()
        if plan_id is None:
            raise PlanNotFound("no plan yet: generate one first")
        try:
            text = self._plan_path(plan_id).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise PlanNotFound(f"no plan {plan_id!r}") from exc
        return parse_plan(text)

    def save_plan(self, plan: Plan) -> None:
        write_text_atomic(self._plan_path(plan.id), dump_plan(plan))
        write_text_atomic(self.data_dir / "current", plan.id + "\n")

    def list_plan_ids(self) -> list[str]:
        if not self.plans_dir.is_dir():
            return []
        return sorted(
            path.stem for path in self.plans_dir.glob("*.json") if PLAN_ID.match(path.stem)
        )

    # ------------------------------------------------------------------ history
    @property
    def history_path(self) -> Path:
        return self.data_dir / "history.json"

    def load_history(self) -> History:
        """history.json, or an empty history before the first sync."""
        try:
            text = self.history_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return History()
        return parse_history(text)

    def save_history(self, history: History) -> None:
        write_text_atomic(self.history_path, dump_history(history))

    # ------------------------------------------------------------------ lock
    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive lock (flock on <data_dir>/.lock, released if the process dies).
        Waits up to lock_timeout_s, then raises StorageBusy."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.data_dir / ".lock", "a+", encoding="utf-8") as handle:
            deadline = time.monotonic() + self.lock_timeout_s
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise StorageBusy(
                        "the data directory is locked by another writer; try again in a moment"
                    )
                time.sleep(LOCK_POLL_S)
            try:
                yield
            finally:
                _unlock(handle)
