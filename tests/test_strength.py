"""Strength library and session builder (F10): data integrity, selection, ladders, fitting."""

import datetime as dt
import itertools

import pytest

from endurance_planner import strength
from endurance_planner.models.enums import Equipment, Pattern, Phase
from endurance_planner.strength import (
    EXERCISES,
    LIBRARY,
    PHASE_TEMPLATE,
    SESSIONS_PER_PHASE,
    TEMPLATES,
    VARIANTS,
    SessionRequest,
    build_session,
    estimate_minutes,
    select_exercise,
)

BW = Equipment.BODYWEIGHT
DEFAULT = frozenset({BW, Equipment.DUMBBELLS, Equipment.BARBELL})
DAY = dt.date(2027, 1, 5)


def request(**changes: object) -> SessionRequest:
    values: dict[str, object] = {
        "date": DAY,
        "phase": Phase.BASE,
        "template_id": "strength_base",
        "level": 0,
        "variant": "A",
        "recovery": False,
        "equipment": DEFAULT,
        "injury": False,
        "minutes": 40,
    }
    values.update(changes)
    return SessionRequest(**values)  # type: ignore[arg-type]


def test_library_is_consistent() -> None:
    assert len(EXERCISES) == len(LIBRARY)
    for spec in LIBRARY:
        assert spec.equipment and spec.rationale
        if spec.harder is not None:
            harder = EXERCISES[spec.harder]
            assert harder.pattern is spec.pattern and harder.harder != spec.id
    for pattern in Pattern:  # every pattern degrades to bodyweight, also with injury notes
        assert select_exercise(pattern, {BW}, injury=True) is not None
    assert not any("jump" in spec.id or "plyo" in spec.id for spec in LIBRARY)


def test_templates_follow_the_roadmap() -> None:
    assert set(PHASE_TEMPLATE) == set(SESSIONS_PER_PHASE) == set(Phase)
    base, build = TEMPLATES["strength_base"], TEMPLATES["strength_build"]
    assert all(2 <= r.sets <= 3 and 10 <= r.reps <= 15 and 3 <= r.rir <= 4 for r in base.ladder)
    assert all(3 <= r.sets <= 5 and 4 <= r.reps <= 8 and 1 <= r.rir <= 3 for r in build.ladder)
    assert all(1 <= r.sets <= 2 for r in TEMPLATES["strength_peak"].ladder)
    assert len(TEMPLATES["strength_light"].ladder) == 1
    assert SESSIONS_PER_PHASE[Phase.BASE] == SESSIONS_PER_PHASE[Phase.BUILD] == 2
    assert SESSIONS_PER_PHASE[Phase.PEAK] == SESSIONS_PER_PHASE[Phase.TAPER] == 1


def selected(pattern: Pattern, available: set[Equipment], **flags: bool) -> str:
    spec = select_exercise(
        pattern, available, injury=flags.get("injury", False), harder=flags.get("harder", False)
    )
    assert spec is not None
    return spec.id


def test_selection_takes_the_first_available_exercise() -> None:
    assert selected(Pattern.SQUAT, set(DEFAULT)) == "goblet_squat"
    assert selected(Pattern.SQUAT, set(DEFAULT), harder=True) == "back_squat"
    only_db = {BW, Equipment.DUMBBELLS}
    assert selected(Pattern.SQUAT, only_db, harder=True) == "goblet_squat"
    assert selected(Pattern.PULL, {BW, Equipment.PULLUP_BAR}) == "pull_up"
    assert selected(Pattern.SQUAT, {BW}) == "bodyweight_squat"


def test_injury_notes_exclude_the_higher_impact_exercises() -> None:
    assert selected(Pattern.SINGLE_LEG, {BW}) == "walking_lunge"
    assert selected(Pattern.SINGLE_LEG, {BW}, injury=True) == "split_squat"
    for pattern, harder in itertools.product(Pattern, (False, True)):
        spec = select_exercise(pattern, {BW}, injury=True, harder=harder)
        assert spec is not None and not spec.high_impact


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_sessions_fit_and_use_only_available_equipment(template_id: str, variant: str) -> None:
    template = TEMPLATES[template_id]
    for level, minutes, equipment in itertools.product(
        range(len(template.ladder)), (20, 40, 60), (DEFAULT, frozenset({BW}))
    ):
        session = build_session(
            request(
                template_id=template_id,
                level=level,
                variant=variant,
                minutes=minutes,
                equipment=equipment,
            )
        )
        assert session is not None
        assert session.minutes <= minutes
        assert estimate_minutes(session.exercises, template) <= minutes
        assert session.rung == level and session.template_id == template_id
        main = [e for e in session.exercises if not e.accessory]
        patterns = list(VARIANTS[variant][0])
        assert [e.pattern for e in main] == patterns[: len(main)]  # the last ones go first
        assert len(main) >= strength.MIN_MAIN_EXERCISES
        if minutes == 60:
            assert len(main) == len(patterns)
        for exercise in session.exercises:
            assert set(EXERCISES[exercise.id].equipment) & equipment
        assert set(session.equipment_used) <= equipment


def test_ladder_prescription_and_recovery_volume() -> None:
    session = build_session(request(level=2, minutes=60))
    assert session is not None and session.name == "Strength foundation A: 3x12"
    main = [e for e in session.exercises if not e.accessory]
    assert {(e.sets, e.reps, e.rir) for e in main} == {(3, 12, 3)}
    recovery = build_session(request(level=2, minutes=60, recovery=True))
    assert recovery is not None and "recovery" in recovery.name
    assert {(e.sets, e.reps, e.rir) for e in recovery.exercises if not e.accessory} == {(2, 12, 4)}
    total = sum(e.sets for e in session.exercises)
    assert sum(e.sets for e in recovery.exercises) <= 0.7 * total  # about -40%


def test_shortening_drops_accessories_first() -> None:
    full = build_session(
        request(template_id="strength_build", phase=Phase.BUILD, level=3, minutes=60)
    )
    short = build_session(
        request(template_id="strength_build", phase=Phase.BUILD, level=3, minutes=35)
    )
    assert full is not None and short is not None
    assert any(e.accessory for e in full.exercises)
    assert not any(e.accessory for e in short.exercises)
    assert [e.id for e in short.exercises] == [e.id for e in full.exercises if not e.accessory]
    assert build_session(request(minutes=strength.MIN_SESSION_MINUTES - 1)) is None


def test_build_phase_uses_heavier_variants() -> None:
    session = build_session(
        request(template_id="strength_build", phase=Phase.BUILD, variant="full", minutes=60)
    )
    assert session is not None
    ids = {e.id for e in session.exercises}
    assert {"back_squat", "rfe_split_squat", "bench_press", "barbell_row"} <= ids
    assert session.equipment_used == [Equipment.DUMBBELLS, Equipment.BARBELL]


def test_description_is_plain_text() -> None:
    session = build_session(request(equipment=frozenset({BW})))
    assert session is not None
    text = strength.description(session)
    assert text.startswith("1. Bodyweight squat, 3 s down: 2 x 15 reps, 4 reps in reserve")
    assert not any(line.startswith("-") for line in text.splitlines())
    assert "Equipment: bodyweight" in text and "not injury advice" in text
