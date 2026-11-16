import pytest

from builders import session, step, two_by_twenty
from endurance_planner.load import weighted_load
from endurance_planner.models.enums import ActivityType, Intent, Sport, StepKind, TargetKind
from endurance_planner.models.plan import Repeat, Step
from endurance_planner.swap import convert_session, convert_target, map_intensity
from endurance_planner.templates import TEMPLATES, key_blocks

IMPACT = 1.25


def test_intensity_mapping_keeps_the_physiological_domain() -> None:
    assert map_intensity(1.0, Sport.BIKE, Sport.RUN) == pytest.approx(1.0)
    assert map_intensity(0.75, Sport.BIKE, Sport.RUN) == pytest.approx(0.84)  # top of low zones
    assert map_intensity(1.20, Sport.BIKE, Sport.RUN) == pytest.approx(1.11)  # VO2max / I pace
    for value in (0.5, 0.65, 0.9, 1.05, 1.3):
        there = map_intensity(value, Sport.BIKE, Sport.RUN)
        assert map_intensity(there, Sport.RUN, Sport.BIKE) == pytest.approx(value)


def test_hr_and_rpe_targets_are_kept() -> None:
    hr = step(600, 0.85, 0.89, TargetKind.HR).target
    rpe = step(60, 8, 9, TargetKind.RPE).target
    assert convert_target(hr, Sport.BIKE, Sport.RUN) is hr
    assert convert_target(rpe, Sport.RUN, Sport.BIKE) is rpe


def test_threshold_session_bike_to_run() -> None:
    """3 x 12 min at 95-100% FTP becomes about 3 x 10 min at run threshold."""
    bike = session(key_blocks(TEMPLATES["bike_threshold"], 2))
    run, warnings = convert_session(bike, IMPACT)
    assert warnings == []
    assert (run.sport, run.activity_type) == (Sport.RUN, ActivityType.RUN)
    assert run.name == "test session [from bike]"
    warmup, main, cooldown = run.steps
    assert isinstance(warmup, Step) and isinstance(cooldown, Step)
    assert (warmup.kind, warmup.duration_s) == (StepKind.WARMUP, 720)  # run standard warm-up
    assert isinstance(main, Repeat) and main.count == 3
    work, rest = main.steps
    assert isinstance(work, Step) and isinstance(rest, Step)
    assert work.duration_s == 570  # 12 min x (0.975 / 0.98)^2 / 1.25, rounded to 30 s
    assert (work.target.kind, work.target.low, work.target.high) == (TargetKind.PACE, 0.96, 1.0)
    assert rest.duration_s < 300
    assert run.duration_s < bike.duration_s


@pytest.mark.parametrize(
    "template_id",
    ["bike_threshold", "bike_sweet_spot", "bike_vo2max", "bike_tempo", "bike_vo2_30_15"],
)
def test_round_trip_keeps_load(template_id: str) -> None:
    bike = session(key_blocks(TEMPLATES[template_id], 2))
    run, _ = convert_session(bike, IMPACT)
    back, _ = convert_session(run, IMPACT)
    assert back.sport is Sport.BIKE
    assert back.name == bike.name
    assert back.tss == pytest.approx(bike.tss, rel=0.03)
    assert back.duration_s == pytest.approx(bike.duration_s, rel=0.05)


def test_main_set_weighted_load_is_preserved_one_way() -> None:
    """Warm-up and cool-down aside (replaced by standard ones), weighted load is kept."""
    bike = session([Repeat(count=3, steps=[step(720, 0.95, 1.0), step(300, 0.5, 0.6)])])
    run, _ = convert_session(bike, IMPACT)
    assert weighted_load(run, IMPACT) == pytest.approx(weighted_load(bike, IMPACT), rel=0.04)
    easy_ride = session([step(3600, 0.60, 0.72)])
    easy_run, _ = convert_session(easy_ride, IMPACT)
    assert easy_run.duration_s == 34 * 60  # a 60 min easy ride is worth a 34 min easy run
    assert weighted_load(easy_run, IMPACT) == pytest.approx(
        weighted_load(easy_ride, IMPACT), rel=0.02
    )


def test_over_unders_become_a_continuous_cruise_block_when_run() -> None:
    bike = session(key_blocks(TEMPLATES["bike_over_unders"], 1))
    run, _ = convert_session(bike, IMPACT)
    main = run.steps[1]
    assert isinstance(main, Repeat) and main.count == 3
    cruise, rest = main.steps
    assert isinstance(cruise, Step) and isinstance(rest, Step)
    assert cruise.note == "continuous"
    assert 9 * 60 <= cruise.duration_s <= 10 * 60  # 12 min of over-unders
    assert 0.9 < cruise.target.low < cruise.target.high < 1.05
    back, _ = convert_session(run, IMPACT)
    assert back.tss == pytest.approx(bike.tss, rel=0.05)


def test_run_to_bike_lengthens_and_drops_distances() -> None:
    reps = Repeat(
        count=5,
        steps=[
            step(240, 1.02, 1.04, TargetKind.PACE, distance_m=1000),
            step(90, 0.66, 0.74, TargetKind.PACE),
        ],
    )
    run = session([reps], Sport.RUN)
    bike, _ = convert_session(run, IMPACT)
    main = bike.steps[0]
    assert isinstance(main, Repeat)
    work = main.steps[0]
    assert isinstance(work, Step)
    assert work.distance_m is None
    assert work.duration_s > 240
    assert work.target.kind is TargetKind.POWER and work.target.low > 1.0
    assert bike.activity_type is ActivityType.RIDE


def test_technical_session_cannot_keep_its_intent() -> None:
    skills = session([step(3600, 0.60, 0.72)]).model_copy(
        update={
            "technical": True,
            "intent": Intent.TECHNICAL,
            "name": "Endurance ride + MTB skills",
        }
    )
    run, warnings = convert_session(skills, IMPACT)
    assert len(warnings) == 1 and "technical session" in warnings[0]
    assert (run.technical, run.intent) == (False, Intent.ENDURANCE)


def test_target_mode_is_applied_to_the_converted_session() -> None:
    run, _ = convert_session(two_by_twenty(), IMPACT, target_mode="hr")
    kinds = {s.target.kind for s in run.flat_steps()}
    assert kinds == {TargetKind.HR}
