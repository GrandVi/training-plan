import datetime as dt

import pytest

from builders import TODAY, goal, make_profile, run_10k_profile
from endurance_planner.models.enums import Phase
from endurance_planner.periodization import (
    PlanningError,
    build_skeleton,
    default_start,
    recovery_flags,
    split_phases,
)

START = dt.date(2026, 11, 16)  # a Monday


def test_default_start_is_next_monday() -> None:
    assert default_start(dt.date(2026, 11, 16)) == dt.date(2026, 11, 16)
    assert default_start(dt.date(2026, 11, 17)) == dt.date(2026, 11, 23)
    assert default_start(dt.date(2026, 11, 22)) == dt.date(2026, 11, 23)


@pytest.mark.parametrize(
    ("weeks", "expected"),
    [(0, (0, 0, 0)), (1, (1, 0, 0)), (3, (1, 1, 1)), (11, (5, 4, 2)), (18, (8, 6, 4))],
)
def test_split_phases(weeks: int, expected: tuple[int, int, int]) -> None:
    assert split_phases(weeks, (0.45, 0.35, 0.20)) == expected
    assert sum(expected) == weeks


def test_recovery_flags_are_anchored_on_the_taper() -> None:
    def pattern(weeks: int, loading: int) -> str:
        return "".join("R" if flag else "L" for flag in recovery_flags(weeks, loading))

    assert pattern(11, 3) == "LLLRLLLRLLL"
    assert pattern(8, 3) == "LLLLRLLL"  # the first week is never a recovery week
    assert pattern(6, 2) == "LLLRLL"
    assert pattern(2, 3) == "LL"
    for weeks in range(1, 30):
        for loading in (2, 3):
            runs = pattern(weeks, loading).split("R")
            assert max(len(run) for run in runs) <= loading + 1
            assert len(runs[-1]) == min(weeks, loading) or weeks <= loading + 1


def test_skeleton_for_a_12_week_10k() -> None:
    weeks, issues = build_skeleton(run_10k_profile(), START)
    assert issues == []
    assert len(weeks) == 12
    assert [week.phase for week in weeks] == (
        [Phase.BASE] * 5 + [Phase.BUILD] * 4 + [Phase.PEAK] * 2 + [Phase.TAPER]
    )
    assert [week.index for week in weeks if week.recovery] == [4, 8]
    assert weeks[-1].race is not None and weeks[-1].race.name == "10k"
    assert weeks[-1].volume_factor == 0.60
    assert all(week.race is None for week in weeks[:-1])
    assert weeks[3].start == START + dt.timedelta(weeks=3)


def test_goal_too_close_is_refused_with_a_suggestion() -> None:
    profile = make_profile(goals=[goal("10k", dt.date(2026, 12, 20), "run_10k")])
    with pytest.raises(PlanningError) as error:
        build_skeleton(profile, START)
    message = str(error.value)
    assert "5 week(s) away" in message
    assert "at least 8" in message
    assert "on or after 2027-01-10" in message  # 8 weeks, same weekday


def test_no_upcoming_goal() -> None:
    profile = make_profile(goals=[goal("old", dt.date(2026, 1, 10), "run_10k")])
    with pytest.raises(PlanningError, match="no goal on or after"):
        build_skeleton(profile, START)


def test_two_main_goals_and_a_minor_race() -> None:
    profile = make_profile(
        goals=[
            goal("10k", dt.date(2027, 2, 7), "run_10k"),
            goal("tune-up 5k", dt.date(2027, 1, 10), "run_5k", priority="B"),
            goal("half", dt.date(2027, 5, 9), "run_half_marathon"),
            goal("later", dt.date(2027, 9, 1), "run_10k", priority="C"),
        ]
    )
    weeks, issues = build_skeleton(profile, START)
    assert [issue.code for issue in issues] == ["goal_not_covered"]
    assert len(weeks) == 12 + 13
    assert [week.goal.name for week in weeks] == ["10k"] * 12 + ["half"] * 13
    assert [goal.name for goal in weeks[7].minor_races] == ["tune-up 5k"]
    second = weeks[12:]
    assert second[0].phase is Phase.TRANSITION
    assert [week.phase for week in second[-2:]] == [Phase.TAPER, Phase.TAPER]
    assert [week.volume_factor for week in second[-2:]] == [0.75, 0.55]
    assert not second[1].recovery  # training resumes with a loading week


def test_compressed_second_macrocycle_warns() -> None:
    profile = make_profile(
        goals=[
            goal("10k", dt.date(2027, 2, 7), "run_10k"),
            goal("10k again", dt.date(2027, 3, 7), "run_10k"),
        ]
    )
    weeks, issues = build_skeleton(profile, START)
    assert [issue.code for issue in issues] == ["compressed_macrocycle"]
    assert [week.phase for week in weeks[12:]] == [
        Phase.TRANSITION,
        Phase.BASE,
        Phase.BUILD,
        Phase.TAPER,
    ]


def test_best_priority_drives_when_there_is_no_a_goal() -> None:
    profile = make_profile(goals=[goal("10k", dt.date(2027, 2, 7), "run_10k", priority="B")])
    weeks, _ = build_skeleton(profile, START)
    assert weeks[-1].race is not None
    assert TODAY == START
