import datetime as dt
from typing import Any

import pytest

from builders import TODAY, session, step, two_by_twenty
from endurance_planner.load import (
    intensity_split,
    low_intensity_ratio,
    project_fitness,
    run_impact_multiplier,
    tss,
    week_weighted_load,
    weighted_load,
)
from endurance_planner.models.enums import Phase, Sport, TargetKind
from endurance_planner.models.plan import Plan, Repeat, Week
from endurance_planner.models.profile import Profile


def test_tss_formula() -> None:
    assert tss(3600, 1.0) == pytest.approx(100)
    assert tss(7200, 0.7) == pytest.approx(98)


def test_session_duration_and_tss() -> None:
    workout = two_by_twenty()
    assert workout.duration_s == 4500
    # 9.0 (warm-up) + 2 x (31.69 + 2.52) + 5.04 (cool-down)
    assert workout.tss == 82.5


def test_nested_repeats() -> None:
    over_unders = session(
        [
            Repeat(
                count=3, steps=[Repeat(count=3, steps=[step(120, 1.05, 1.10), step(120, 0.9, 0.9)])]
            )
        ]
    )
    assert len(list(over_unders.flat_steps())) == 18
    assert over_unders.duration_s == 2160
    assert over_unders.tss == pytest.approx(9 * (tss(120, 1.075) + tss(120, 0.9)), abs=0.05)


def test_run_tss_from_pace_hr_and_rpe() -> None:
    assert session([step(3600, 1.0, 1.0, TargetKind.PACE)], Sport.RUN).tss == 100
    # 89% LTHR maps to IF 0.84 (run) / 0.75 (bike)
    assert session([step(3600, 0.89, 0.89, TargetKind.HR)], Sport.RUN).tss == 70.6
    assert session([step(3600, 0.89, 0.89, TargetKind.HR)], Sport.BIKE).tss == 56.2
    assert session([step(3600, 4, 4, TargetKind.RPE)], Sport.RUN).tss == 70.6


def test_intensity_split() -> None:
    workout = two_by_twenty()
    assert intensity_split(workout) == (2100, 2400)
    easy_run = session([step(3600, 0.75, 0.83, TargetKind.PACE)], Sport.RUN)
    assert intensity_split(easy_run) == (3600, 0)
    tempo_run = session([step(1800, 0.90, 0.94, TargetKind.PACE)], Sport.RUN)
    assert intensity_split(tempo_run) == (0, 1800)
    assert low_intensity_ratio([workout, easy_run, tempo_run]) == pytest.approx(5700 / 9900)
    assert low_intensity_ratio([]) == 1.0


def test_split_boundaries_per_target_kind() -> None:
    def low_s(low: float, high: float, kind: TargetKind, sport: Sport) -> int:
        return intensity_split(session([step(60, low, high, kind)], sport))[0]

    assert low_s(0.75, 0.75, TargetKind.POWER, Sport.BIKE) == 60
    assert low_s(0.76, 0.76, TargetKind.POWER, Sport.BIKE) == 0
    assert low_s(0.89, 0.89, TargetKind.HR, Sport.RUN) == 60
    assert low_s(0.90, 0.90, TargetKind.HR, Sport.RUN) == 0
    assert low_s(4, 4, TargetKind.RPE, Sport.BIKE) == 60
    assert low_s(5, 5, TargetKind.RPE, Sport.BIKE) == 0


def test_run_impact_weighting(profile: Profile, profile_data: dict[str, Any]) -> None:
    assert run_impact_multiplier(profile) == 1.25
    profile_data["athlete"]["injury_notes"] = "left Achilles, 2025"
    assert run_impact_multiplier(Profile.model_validate(profile_data)) == pytest.approx(1.40)

    ride = session([step(3600, 1.0, 1.0)])
    run = session([step(3600, 1.0, 1.0, TargetKind.PACE)], Sport.RUN)
    assert weighted_load(ride, 1.25) == 100
    assert weighted_load(run, 1.25) == 125
    week = Week(index=1, start=dt.date(2026, 11, 30), phase=Phase.BASE, sessions=[ride, run])
    assert week.tss == 200  # displayed TSS is not weighted
    assert week.duration_s == 7200
    assert week_weighted_load(week, 1.25) == 225


def test_project_fitness() -> None:
    points = project_fitness([100.0] * 400)
    assert points[0].ctl == pytest.approx(100 / 42)
    assert points[0].atl == pytest.approx(100 / 7)
    assert points[0].tsb == 0
    assert points[1].tsb == pytest.approx(100 / 42 - 100 / 7)
    assert points[-1].ctl == pytest.approx(100, abs=0.1)
    assert points[-1].tsb == pytest.approx(0, abs=0.1)
    rested = project_fitness([0.0] * 7, ctl0=80, atl0=100)
    assert rested[0].tsb == -20
    assert rested[-1].tsb > 0  # ATL decays faster than CTL


def test_plan_json_round_trip(profile: Profile) -> None:
    week = Week(index=1, start=dt.date(2026, 11, 30), phase=Phase.BASE, sessions=[two_by_twenty()])
    plan = Plan(id="p1", created=TODAY, athlete="Victor", goals=profile.goals, weeks=[week])
    payload = plan.model_dump_json()
    assert '"tss":82.5' in payload
    assert '"duration_s":4500' in payload
    assert Plan.model_validate_json(payload) == plan
