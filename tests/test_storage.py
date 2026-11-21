"""FileStorage: atomic writes, lock, plan ids, the profile is never replaced."""

import os
import threading
import time
from pathlib import Path

import pytest

from builders import TODAY, make_profile
from endurance_planner.planner import generate_plan
from endurance_planner.serialization import ProfileError
from endurance_planner.services import PlanNotFound, StorageBusy
from endurance_planner.storage import FileStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(tmp_path / "data", lock_timeout_s=0.2)


def test_save_and_load(storage: FileStorage) -> None:
    plan = generate_plan(make_profile(), TODAY)
    storage.save_plan(plan)
    assert storage.load_plan() == plan == storage.load_plan(plan.id)
    assert storage.list_plan_ids() == [plan.id] and storage.current_plan_id() == plan.id
    with pytest.raises(PlanNotFound, match="no plan 'other-plan'"):
        storage.load_plan("other-plan")
    with pytest.raises(PlanNotFound, match="invalid plan id"):
        storage.load_plan("../../etc/passwd")


def test_a_crash_before_the_rename_keeps_the_old_file(
    storage: FileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = generate_plan(make_profile(), TODAY)
    storage.save_plan(plan)
    path = storage.plans_dir / f"{plan.id}.json"
    before = path.read_bytes()
    changed = plan.model_copy(update={"revision": 7})

    def crash(source: str, target: str) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)  # used by files.write_text_atomic
    with pytest.raises(OSError, match="power cut"):
        storage.save_plan(changed)
    assert path.read_bytes() == before  # the old content, complete
    assert sorted(p.name for p in storage.plans_dir.iterdir()) == [path.name]  # temp removed
    monkeypatch.undo()
    # A process killed between the write and the rename leaves a temporary file behind:
    # it is not a plan and does not hide the real one.
    (storage.plans_dir / f".{plan.id}.json.abc.tmp").write_text("{half", encoding="utf-8")
    assert storage.list_plan_ids() == [plan.id] and storage.load_plan() == plan
    storage.save_plan(changed)
    assert storage.load_plan().revision == 7


def test_lock_excludes_other_writers(storage: FileStorage) -> None:
    other = FileStorage(storage.data_dir, lock_timeout_s=0.1)
    with storage.lock():
        started = time.monotonic()
        with pytest.raises(StorageBusy, match="locked by another writer"), other.lock():
            pass
        assert time.monotonic() - started >= 0.1
    with other.lock():  # released
        pass
    with pytest.raises(RuntimeError), storage.lock():
        raise RuntimeError("boom")
    with other.lock():  # released after an error too
        pass


def test_lock_waits_for_the_holder(storage: FileStorage) -> None:
    waiting = FileStorage(storage.data_dir, lock_timeout_s=5)
    order: list[str] = []
    held = threading.Event()

    def holder() -> None:
        with storage.lock():
            held.set()
            time.sleep(0.15)
            order.append("first")

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait()
    with waiting.lock():
        order.append("second")
    thread.join()
    assert order == ["first", "second"]


def test_profile_is_read_not_replaced(storage: FileStorage) -> None:
    with pytest.raises(ProfileError, match="no profile.yaml"):
        storage.load_profile()
    profile = make_profile()
    storage.save_profile(profile)  # setup of an empty data dir
    assert storage.load_profile() == profile
    text = storage.profile_path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="edited by hand"):
        storage.save_profile(profile)
    assert storage.profile_path.read_text(encoding="utf-8") == text
    storage.profile_path.write_text("athlete: [", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid YAML"):
        storage.load_profile()
    assert os.access(storage.data_dir, os.W_OK)
