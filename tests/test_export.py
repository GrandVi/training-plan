import csv
import io

import pytest

from builders import TODAY, run_10k_profile, two_by_twenty
from endurance_planner.export.json_io import dump_plan, load_plan, save_plan
from endurance_planner.export.markdown import (
    fitness_csv,
    format_duration,
    render_markdown,
    step_lines,
    weekly_csv,
)
from endurance_planner.models.plan import Plan
from endurance_planner.planner import generate_plan
from endurance_planner.targets import resolve_session
from endurance_planner.thresholds import ResolvedThresholds


@pytest.fixture(scope="module")
def plan() -> Plan:
    return generate_plan(run_10k_profile(), TODAY)


def test_format_duration() -> None:
    assert [format_duration(s) for s in (45, 720, 90, 5400, 3600, 0)] == [
        "45s",
        "12m",
        "1m30s",
        "1h30m",
        "1h",
        "0s",
    ]


def test_step_lines_show_relative_and_absolute_targets(thresholds: ResolvedThresholds) -> None:
    workout = resolve_session(two_by_twenty(), thresholds)
    assert step_lines(workout.steps) == [
        "- Warm-up 15m @ 55-65% FTP (143-169 W, 134-142 bpm)",
        "- 2x",
        "  - 20m @ 95-100% FTP (247-260 W, 162-168 bpm)",
        "  - Recovery 5m @ 50-60% FTP (130-156 W, 123-138 bpm)",
        "- Cool-down 10m @ 50-60% FTP (130-156 W, 123-138 bpm)",
    ]


def test_json_round_trip(plan: Plan, tmp_path_factory: pytest.TempPathFactory) -> None:
    path = tmp_path_factory.mktemp("plan") / "plan.json"
    save_plan(plan, path)
    assert load_plan(path) == plan
    assert dump_plan(load_plan(path)) == path.read_text(encoding="utf-8")


def test_markdown_summary(plan: Plan) -> None:
    text = render_markdown(plan, run_10k_profile())
    assert text.startswith("# Training plan: Victor\n")
    assert "- 2027-02-07 [A] 10k (run_10k, target 41:00)" in text
    assert "- Run: threshold pace 4:15 /km, LTHR 172 bpm" in text
    assert "- Bike: FTP 260 W (3.6 W/kg), LTHR 168 bpm" in text
    assert "pace_race_mismatch" in text
    table = [line for line in text.splitlines() if line.startswith("| ")]
    assert len(table) == 1 + 12
    assert table[0].startswith(
        "| Wk | Start | Phase | Hours | Bike | Run | TSS | Load | Low/High |"
    )
    assert "| 4 | 2026-12-07 | base (rec) |" in text
    assert "RACE: 10k" in table[-1]
    assert text.count("\n## Week ") == 12
    assert "Why: " in text and "threshold pace (" in text


def test_markdown_week_filter_and_table_only(plan: Plan) -> None:
    profile = run_10k_profile()
    one = render_markdown(plan, profile, weeks=[3])
    assert one.count("\n## Week ") == 1 and "## Week 3 - base (2026-11-30)" in one
    assert "\n## Week " not in render_markdown(plan, profile, details=False)


def test_weekly_csv(plan: Plan) -> None:
    rows = list(csv.DictReader(io.StringIO(weekly_csv(plan, run_10k_profile()))))
    assert len(rows) == 12
    first = rows[0]
    assert (first["week"], first["start"], first["phase"]) == ("1", "2026-11-16", "base")
    assert float(first["hours"]) == pytest.approx(plan.weeks[0].duration_s / 3600, abs=0.05)
    assert int(first["low_pct"]) + int(first["high_pct"]) == 100
    assert rows[3]["recovery"] == "yes"
    assert rows[-1]["races"] == "10k"


def test_fitness_csv(plan: Plan) -> None:
    rows = list(csv.DictReader(io.StringIO(fitness_csv(plan, run_10k_profile()))))
    assert len(rows) == 12 * 7
    assert rows[0]["date"] == "2026-11-16"
    assert sum(float(row["tss"]) for row in rows) == pytest.approx(sum(w.tss for w in plan.weeks))
    # Form rises through the taper.
    assert float(rows[-1]["tsb"]) > float(rows[-10]["tsb"])


def test_example_plan_markdown_is_up_to_date() -> None:
    """examples/plan.example.md is the golden file of the full pipeline on the example profile."""
    from builders import EXAMPLE, make_profile

    profile = make_profile()
    text = render_markdown(generate_plan(profile, TODAY), profile, weeks=[1, 9, 26])
    assert text == (EXAMPLE.parent / "plan.example.md").read_text(encoding="utf-8")
