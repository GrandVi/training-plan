"""Service layer: same results as the library, dry runs, revisions, storage-backed service."""

import datetime as dt
from pathlib import Path

import pytest

from builders import TODAY, run_10k_profile
from endurance_planner import amend as amend_lib
from endurance_planner import planner, replan, services, validation
from endurance_planner.export.intervals_icu import plan_events
from endurance_planner.export.markdown import render_markdown
from endurance_planner.models.enums import BikeActivity, Scope, Sport
from endurance_planner.models.plan import Plan
from endurance_planner.serialization import dump_plan
from endurance_planner.services import (
    AmendRequest,
    PlannerService,
    PushRequest,
    RevisionConflict,
    UnavailableRequest,
)
from endurance_planner.storage import FileStorage
from fake_intervals import FakeIntervals

PROFILE = run_10k_profile()
KEY = dt.date(2026, 12, 22)  # week 6 tempo key session
LONG = dt.date(2026, 12, 26)


@pytest.fixture(scope="module")
def plan() -> Plan:
    return planner.generate_plan(PROFILE, TODAY)


def _norm(plan: Plan) -> Plan:
    return plan.model_copy(update={"revision": 0})


# ---------------------------------------------------------------------- same as the library
def test_results_equal_the_library_calls(plan: Plan) -> None:
    generated = services.generate_plan(PROFILE, TODAY)
    assert generated.candidate == plan and generated.stored == plan
    assert generated.result.message == "12 weeks from 2026-11-16"

    request = AmendRequest(date=KEY, minutes=40, reason="busy")
    outcome = services.amend(plan, PROFILE, TODAY, request, expected_revision=0)
    direct = amend_lib.amend_session(
        plan, PROFILE, TODAY, KEY, amend_lib.SessionEdit(minutes=40), reason="busy"
    )
    assert outcome.stored is not None and _norm(outcome.stored) == direct.plan
    assert [c.line for c in outcome.result.changes] == [c.describe() for c in direct.changes]
    assert outcome.result.issues == direct.issues
    assert outcome.result.message == "amendment a1: 40 min (scope rest)"

    unavailable = UnavailableRequest.model_validate(
        {"from": "2026-12-21", "to": "2026-12-27", "minutes": {"run": 0, "bike": 60}}
    )
    declared = services.declare_unavailable(plan, PROFILE, TODAY, unavailable, expected_revision=0)
    expected = amend_lib.add_unavailable(
        plan,
        PROFILE,
        TODAY,
        KEY - dt.timedelta(days=1),
        KEY + dt.timedelta(days=5),
        {Sport.RUN: 0, Sport.BIKE: 60},
    )
    assert declared.stored is not None and _norm(declared.stored) == expected.plan

    later = dt.date(2026, 12, 10)
    replanned = services.replan(plan, PROFILE, later, expected_revision=0)
    assert _norm(replanned.candidate) == replan.replan_plan(plan, PROFILE, later).plan

    undone = services.undo_amendment(direct.plan, PROFILE, TODAY, "a1", expected_revision=0)
    assert undone.stored is not None
    assert _norm(undone.stored) == amend_lib.undo_amendment(direct.plan, PROFILE, TODAY, "a1").plan

    swapped = services.swap(plan, PROFILE, TODAY, LONG + dt.timedelta(days=1), expected_revision=0)
    assert (
        _norm(swapped.candidate)
        == amend_lib.swap_sport(plan, PROFILE, TODAY, LONG + dt.timedelta(days=1)).plan
    )

    report = services.validate_plan(plan, PROFILE)
    assert report.issues == validation.validate_plan(plan, PROFILE) and report.errors == 0
    assert services.render_plan(plan, PROFILE) == render_markdown(plan, PROFILE)
    assert [v.line for v in services.list_amendments(direct.plan)] == amend_lib.amendment_lines(
        direct.plan
    )
    push = services.push_plan(plan, None, TODAY, PushRequest(dry_run=True))
    assert push.payloads == plan_events(plan, TODAY) and push.events == len(push.payloads)


def test_views(plan: Plan) -> None:
    overview = services.plan_overview(plan, PROFILE, dt.date(2026, 12, 23))
    assert (overview.current_week, len(overview.weeks), overview.revision) == (6, 12, 0)
    assert overview.weeks[5].key_sessions == ["Cruise intervals 4x8 min", "VO2max reps 5x2 min"]
    assert services.plan_overview(plan, PROFILE, dt.date(2030, 1, 1)).current_week is None
    week = services.show_week(plan, PROFILE, 6)
    assert (week.index, week.weeks, week.phase.value) == (6, 12, "build")
    key = next(s for s in week.sessions if s.date == KEY)
    assert key.hard and key.rationale and key.steps
    work = [line for line in key.steps if line.kind and line.kind.value == "work"]
    assert work and all(
        line.absolute.endswith("/km, 159-170 bpm") or line.absolute for line in work
    )
    assert any(line.repeat for line in key.steps)
    with pytest.raises(services.UnknownWeek, match="weeks 1 to 12"):
        services.show_week(plan, PROFILE, 13)
    summary = services.profile_summary(PROFILE, TODAY)
    assert {t.name for t in summary.thresholds} >= {"Bike FTP", "Run threshold pace"}
    assert summary.zones and summary.athlete == "Victor"


# ---------------------------------------------------------------------- dry run and revisions
def test_dry_run_changes_nothing(plan: Plan) -> None:
    before = plan.model_copy(deep=True)
    request = AmendRequest(date=KEY, skip=True)
    outcome = services.amend(plan, PROFILE, TODAY, request, expected_revision=0, dry_run=True)
    assert outcome.stored is None and outcome.result.dry_run and outcome.result.applied
    assert outcome.result.revision == 0 and outcome.result.changes
    assert outcome.candidate.revision == 1  # what would be stored
    assert plan == before


def test_revisions(plan: Plan) -> None:
    first = services.amend(
        plan, PROFILE, TODAY, AmendRequest(date=KEY, minutes=40), expected_revision=0
    )
    assert first.stored is not None and first.stored.revision == first.result.revision == 1
    with pytest.raises(RevisionConflict) as conflict:
        services.amend(
            first.stored, PROFILE, TODAY, AmendRequest(date=LONG, minutes=60), expected_revision=0
        )
    assert (conflict.value.expected, conflict.value.current) == (0, 1)
    with pytest.raises(RevisionConflict):  # dry runs are checked too
        services.replan(first.stored, PROFILE, TODAY, expected_revision=5, dry_run=True)
    same = services.amend(
        first.stored, PROFILE, TODAY, AmendRequest(date=KEY, minutes=40), expected_revision=1
    )
    assert same.stored is not None and same.stored.revision == 1 and not same.result.changed
    unchecked = services.replan(first.stored, PROFILE, TODAY, expected_revision=None)
    assert unchecked.result.revision == 1  # nothing changed: same revision
    edit = AmendRequest(date=LONG, sport=Sport.BIKE, activity=BikeActivity.MTB, minutes=150)
    refused = services.amend(first.stored, PROFILE, TODAY, edit, expected_revision=1)
    assert refused.stored is None and refused.result.refused and refused.result.revision == 1
    assert [i.code for i in refused.result.errors] == ["long_session_ramp"]
    forced = services.amend(first.stored, PROFILE, TODAY, edit, expected_revision=1, force=True)
    assert forced.stored is not None and forced.stored.revision == 2


# ---------------------------------------------------------------------- service on storage
def _service(tmp_path: Path, fake: FakeIntervals | None = None) -> PlannerService:
    data = tmp_path / "data"
    data.mkdir()
    example = Path(__file__).parent.parent / "examples" / "profile.example.yaml"
    (data / "profile.yaml").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return PlannerService(FileStorage(data), fake.client if fake else None)


def test_planner_service_on_files(tmp_path: Path) -> None:
    fake = FakeIntervals()
    service = _service(tmp_path, fake)
    storage = service.storage
    assert isinstance(storage, FileStorage)
    with pytest.raises(services.PlanNotFound):
        service.plan()
    created = service.generate(TODAY, expected_revision=None)
    assert created.revision == 0 and storage.list_plan_ids() == ["victor-20261116"]
    path = storage.plans_dir / "victor-20261116.json"
    text = path.read_text(encoding="utf-8")
    key = next(s for s in service.week(4).sessions if s.hard)
    preview = service.amend(
        TODAY, AmendRequest(date=key.date, minutes=30), expected_revision=0, dry_run=True
    )
    assert preview.changes and path.read_text(encoding="utf-8") == text
    applied = service.amend(TODAY, AmendRequest(date=key.date, minutes=30), expected_revision=0)
    assert applied.revision == 1 and service.plan().revision == 1
    with pytest.raises(RevisionConflict):
        service.replan(TODAY, expected_revision=0)
    again = service.generate(TODAY, expected_revision=1)
    assert again.revision == 2 and service.plan().amendments == []  # a fresh plan
    undone = service.declare_unavailable(
        TODAY, UnavailableRequest.model_validate({"from": "2026-12-21"}), expected_revision=2
    )
    assert undone.revision == 3 and service.amendments()[0].kind.value == "unavailable"
    assert service.undo(TODAY, "a1", expected_revision=3).revision == 4
    assert service.overview(TODAY).revision == 4
    # Push: dry run without network, then for real through the fake intervals.icu.
    dry = service.push(TODAY, PushRequest(dry_run=True))
    assert dry.payloads and fake.transport.requests == []
    pushed = service.push(TODAY, PushRequest())
    assert pushed.upserted == dry.events == len(fake.events)
    assert pushed.threshold_diffs == [
        "bike FTP (W): profile 260, intervals.icu 265",
        "run threshold pace: profile 4:15, intervals.icu 4:10",
    ]
    assert service.push(TODAY, PushRequest()).unchanged == dry.events
    no_client = PlannerService(storage)
    with pytest.raises(ValueError, match="not configured"):
        no_client.push(TODAY, PushRequest())
    assert (
        (storage.data_dir / "profile.yaml")
        .read_text(encoding="utf-8")
        .startswith("# yaml-language-server")
    )  # never written


def test_scope_and_revision_survive_a_reload(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.generate(TODAY, expected_revision=None)
    week = service.week(5)
    ride = next(s for s in week.sessions if s.sport is Sport.BIKE)
    result = service.amend(
        TODAY,
        AmendRequest(date=ride.date, sport=Sport.RUN, scope=Scope.SESSION),
        expected_revision=0,
    )
    stored = FileStorage(
        service.storage.data_dir if isinstance(service.storage, FileStorage) else tmp_path
    ).load_plan()
    assert stored.revision == result.revision == 1
    assert '"revision": 1' in dump_plan(stored)
