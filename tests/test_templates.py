import pytest

from endurance_planner.load import is_low_intensity
from endurance_planner.models.enums import Phase, Sport, StepKind, TargetKind
from endurance_planner.models.plan import Repeat, Step, flatten
from endurance_planner.specificity import GOAL_SPECS
from endurance_planner.templates import (
    OPENERS,
    TEMPLATES,
    Template,
    key_blocks,
    long_blocks,
    main_set,
    rung_label,
    steady_blocks,
)


def _work_s(template: Template, level: int) -> int:
    """Seconds above LT1 in the main set (distance reps at an assumed 5:00 /km threshold)."""
    steps = flatten(main_set(template, level, pace_s=300))
    return sum(s.duration_s for s in steps if not is_low_intensity(s.target, template.sport))


@pytest.mark.parametrize("template", TEMPLATES.values(), ids=lambda t: t.id)
def test_ladders_progress(template: Template) -> None:
    work = [_work_s(template, level) for level in range(len(template.ladder))]
    assert work[0] > 0
    assert work == sorted(work), "each rung must give at least as much work as the previous one"
    labels = [rung.label for rung in template.ladder]
    assert len(set(labels)) == len(labels)
    assert template.rationale and template.phases
    for prerequisite in template.prerequisites:
        assert prerequisite in TEMPLATES


def test_specs_reference_valid_templates() -> None:
    for goal_type, spec in GOAL_SPECS.items():
        assert 0.99 < sum(spec.phase_split) < 1.01
        assert spec.taper_factors == tuple(sorted(spec.taper_factors, reverse=True))
        assert all(0.5 <= factor <= 0.8 for factor in spec.taper_factors)
        for sport, by_phase in spec.keys.items():
            assert OPENERS[sport] in TEMPLATES
            for phase, rotations in by_phase.items():
                for rotation in rotations:
                    for template_id in rotation:
                        template = TEMPLATES[template_id]
                        assert template.sport is sport, (goal_type, template_id)
                        assert phase in template.phases, (goal_type, phase, template_id)
        # Every rotation must have at least one template without prerequisites reachable:
        # the first base key is always available.
        for by_phase in spec.keys.values():
            first = by_phase[Phase.BASE][0]
            assert any(not TEMPLATES[t].prerequisites for t in first)


def test_main_set_structure() -> None:
    threshold = main_set(TEMPLATES["bike_threshold"], 2)
    assert len(threshold) == 1 and isinstance(threshold[0], Repeat)
    assert threshold[0].count == 3
    work, rest = threshold[0].steps
    assert isinstance(work, Step) and isinstance(rest, Step)
    assert (work.duration_s, work.target.low, work.target.high) == (720, 0.95, 1.00)
    assert (rest.kind, rest.duration_s, rest.target.high) == (StepKind.RECOVERY, 300, 0.55)
    assert rung_label(TEMPLATES["bike_threshold"], 2) == "3x12 min"
    # Levels beyond the ladder stay on the last rung.
    assert main_set(TEMPLATES["bike_threshold"], 99) == main_set(TEMPLATES["bike_threshold"], 5)


def test_nested_sets() -> None:
    over_unders = main_set(TEMPLATES["bike_over_unders"], 1)  # 3 x 3 x (2 + 2 min), 5 min rest
    outer = over_unders[0]
    assert isinstance(outer, Repeat) and outer.count == 3
    inner, rest = outer.steps
    assert isinstance(inner, Repeat) and inner.count == 3
    assert isinstance(rest, Step) and rest.duration_s == 300
    assert sum(step.duration_s for step in flatten(over_unders)) == 3 * (3 * 240 + 300)


def test_distance_reps_and_goal_pace() -> None:
    template = TEMPLATES["run_race_pace"]
    default = main_set(template, 0, pace_s=255)[0]
    assert isinstance(default, Repeat) and default.count == 5
    rep = default.steps[0]
    assert isinstance(rep, Step)
    assert rep.distance_m == 1000
    assert rep.duration_s == round(255 / 1.03)  # default 101-105% of threshold speed
    with_goal = main_set(template, 0, pace_s=255, goal_pace=1.037)[0]
    assert isinstance(with_goal, Repeat)
    goal_rep = with_goal.steps[0]
    assert isinstance(goal_rep, Step)
    assert (goal_rep.target.low, goal_rep.target.high) == (1.027, 1.047)


def test_effort_based_reps_use_rpe() -> None:
    hills = main_set(TEMPLATES["run_hills"], 0)[0]
    assert isinstance(hills, Repeat)
    rep = hills.steps[0]
    assert isinstance(rep, Step)
    assert rep.target.kind is TargetKind.RPE
    assert "uphill" in rep.note


def test_key_blocks_have_warmup_and_cooldown() -> None:
    blocks = key_blocks(TEMPLATES["run_tempo"], 0)
    first, last = blocks[0], blocks[-1]
    assert isinstance(first, Step) and isinstance(last, Step)
    assert (first.kind, first.duration_s) == (StepKind.WARMUP, 720)
    assert (last.kind, last.duration_s) == (StepKind.COOLDOWN, 480)
    short = key_blocks(TEMPLATES["run_tempo"], 0, short=True)
    assert sum(s.duration_s for s in flatten(short)) < sum(s.duration_s for s in flatten(blocks))


def test_steady_and_long_blocks() -> None:
    easy = steady_blocks(Sport.RUN, 2400)
    assert sum(step.duration_s for step in flatten(easy)) == 2400
    for sport, duration in ((Sport.BIKE, 3 * 3600), (Sport.BIKE, 2 * 3600), (Sport.RUN, 80 * 60)):
        plain = list(flatten(long_blocks(sport, duration, quality=False)))
        quality = list(flatten(long_blocks(sport, duration, quality=True)))
        assert sum(step.duration_s for step in plain) == duration
        assert sum(step.duration_s for step in quality) == duration
        assert all(is_low_intensity(step.target, sport) for step in plain)
        assert any(not is_low_intensity(step.target, sport) for step in quality)
    # Too short for a quality block: stays plain.
    assert len(long_blocks(Sport.BIKE, 5400, quality=True)) == 1
