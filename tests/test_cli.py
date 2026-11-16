import json
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from builders import EXAMPLE, example_data
from endurance_planner import cli
from endurance_planner.cli import app
from endurance_planner.config import load_profile
from endurance_planner.export.intervals_icu import IntervalsClient
from endurance_planner.export.json_io import load_plan
from test_intervals_icu import FakeApi

runner = CliRunner()
TODAY = ["--today", "2026-11-16"]


@pytest.fixture(scope="module")
def profile_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The example profile with the 10k goal only."""
    data = example_data()
    data["goals"] = data["goals"][:1]
    path = tmp_path_factory.mktemp("profile") / "profile.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def plan_path(profile_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("cli")
    out = folder / "plan.json"
    args = [
        "plan",
        "generate",
        "-p",
        str(profile_path),
        "-o",
        str(out),
        "-m",
        str(folder / "plan.md"),
    ]
    result = runner.invoke(app, [*args, *TODAY])
    assert result.exit_code == 0, result.output
    return out


def test_profile_validate_prints_zone_tables() -> None:
    result = runner.invoke(app, ["profile", "validate", "-p", str(EXAMPLE), *TODAY])
    assert result.exit_code == 0
    assert "[warning] pace_race_mismatch" in result.output
    assert "Z4 Threshold          235-273 W" in result.output
    assert "T Threshold           4:08-4:26 /km" in result.output


def test_profile_validate_reports_errors(tmp_path: Path) -> None:
    bad = tmp_path / "profile.yaml"
    bad.write_text(EXAMPLE.read_text().replace('name: "Victor"', "nmae: x"), encoding="utf-8")
    result = runner.invoke(app, ["profile", "validate", "-p", str(bad)])
    assert result.exit_code == 1
    assert "athlete.name" in result.output and "athlete.nmae" in result.output
    missing = runner.invoke(app, ["profile", "validate", "-p", str(tmp_path / "none.yaml")])
    assert missing.exit_code == 1


def test_profile_init_writes_a_valid_profile(tmp_path: Path) -> None:
    out = tmp_path / "profile.yaml"
    answers = ["Ana", "250", "4:30", "5", "2", "5", "8", "Spring 10k", "2027-04-11", "run_10k"]
    result = runner.invoke(
        app, ["profile", "init", "-o", str(out)], input="\n".join(answers) + "\n"
    )
    assert result.exit_code == 0, result.output
    profile = load_profile(out)
    assert profile.athlete.name == "Ana"
    assert profile.thresholds.run is not None and profile.thresholds.run.threshold_pace == 270
    assert profile.availability.days_per_week == 5
    assert len(profile.availability.training_days()) == 5
    assert profile.goals[0].name == "Spring 10k"


def test_plan_generate_writes_json_and_markdown(plan_path: Path) -> None:
    plan = load_plan(plan_path)
    assert plan.created.isoformat() == "2026-11-16"
    assert plan.weeks[-1].races
    markdown = plan_path.with_suffix(".md").read_text(encoding="utf-8")
    assert markdown.startswith("# Training plan: Victor")


def test_plan_generate_refuses_a_goal_that_is_too_close(profile_path: Path, tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        profile_path.read_text().replace("2027-03-14", "2026-12-06"), encoding="utf-8"
    )
    result = runner.invoke(
        app, ["plan", "generate", "-p", str(profile), "-o", str(tmp_path / "p.json"), *TODAY]
    )
    assert result.exit_code == 1
    assert "needs at least 8" in result.output
    assert not (tmp_path / "p.json").exists()


def test_plan_show(plan_path: Path, profile_path: Path) -> None:
    base = ["plan", "show", str(plan_path), "-p", str(profile_path)]
    week = runner.invoke(app, [*base, "--week", "2"])
    assert week.exit_code == 0
    assert week.output.count("\n## Week ") == 1
    table = runner.invoke(app, [*base, "--no-details"])
    assert "\n## Week " not in table.output
    weekly = runner.invoke(app, [*base, "-f", "csv"])
    assert weekly.output.startswith("week,start,phase,")
    fitness = runner.invoke(app, [*base, "-f", "fitness-csv"])
    assert fitness.output.startswith("date,tss,ctl,atl,tsb\n")
    assert runner.invoke(app, [*base, "-f", "pdf"]).exit_code == 1


def test_plan_validate(plan_path: Path, profile_path: Path) -> None:
    ok = runner.invoke(app, ["plan", "validate", str(plan_path), "-p", str(profile_path)])
    assert ok.exit_code == 0
    assert "0 error(s)" in ok.output
    late = runner.invoke(
        app, ["plan", "validate", str(plan_path), "-p", str(profile_path), "--today", "2026-12-25"]
    )
    assert late.exit_code == 1
    assert "session_in_past" in late.output


def test_plan_retarget(plan_path: Path, profile_path: Path, tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        profile_path.read_text().replace("ftp_w: 260", "ftp_w: 275"), encoding="utf-8"
    )
    out = tmp_path / "retargeted.json"
    result = runner.invoke(
        app, ["plan", "retarget", str(plan_path), "-p", str(profile), "-o", str(out), *TODAY]
    )
    assert result.exit_code == 0, result.output
    before, after = load_plan(plan_path), load_plan(out)
    old = [s for week in before.weeks for s in week.sessions]
    new = [s for week in after.weeks for s in week.sessions]
    assert [(s.date, s.name, s.duration_s) for s in new] == [
        (s.date, s.name, s.duration_s) for s in old
    ]
    watts_before = {step.resolved.power_w for s in old for step in s.flat_steps() if step.resolved}
    watts_after = {step.resolved.power_w for s in new for step in s.flat_steps() if step.resolved}
    assert watts_before != watts_after


def test_plan_swap(plan_path: Path, profile_path: Path, tmp_path: Path) -> None:
    plan = load_plan(plan_path)
    ride = next(
        s for s in plan.weeks[5].sessions if s.sport.value == "bike" and s.duration_s >= 5400
    )
    base = [
        "plan",
        "swap",
        str(plan_path),
        "--date",
        ride.date.isoformat(),
        "-p",
        str(profile_path),
    ]
    out = tmp_path / "swapped.json"
    refused = runner.invoke(app, [*base, "-o", str(out), *TODAY])
    assert refused.exit_code == 1  # one more run breaks the run-volume cap
    assert "run_volume_ramp" in refused.output and "--force" in refused.output
    assert not out.exists()
    forced = runner.invoke(app, [*base, "-o", str(out), "--force", *TODAY])
    assert forced.exit_code == 0, forced.output
    swapped = next(s for s in load_plan(out).weeks[5].sessions if s.date == ride.date)
    assert swapped.sport.value == "run"
    assert "now a run session" in forced.output
    nothing = runner.invoke(
        app, ["plan", "swap", str(plan_path), "--date", "2026-11-16", "-p", str(profile_path)]
    )
    assert nothing.exit_code == 1 and "no session on" in nothing.output


# ---------------------------------------------------------------------- intervals.icu
@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()

    def make_client(athlete_id: str | None, verbose: bool) -> IntervalsClient:
        return IntervalsClient("key", athlete_id or "0", transport=httpx.MockTransport(api))

    monkeypatch.setattr(cli, "make_client", make_client)
    return api


def test_push_dry_run_prints_payloads_without_network(
    plan_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(athlete_id: str | None, verbose: bool) -> IntervalsClient:
        raise AssertionError("dry run must not build a client")

    monkeypatch.setattr(cli, "make_client", no_network)
    args = ["push", str(plan_path), "--dry-run", "--from", "2026-11-23", "--to", "2026-11-29"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    payloads = json.loads(result.output)
    assert len(payloads) == len(load_plan(plan_path).weeks[1].sessions)
    assert all(p["category"] == "WORKOUT" and "%" in p["description"] for p in payloads)
    assert payloads[0]["external_id"].startswith("endurance-planner:victor-20261116:2026-11-2")
    absolute = runner.invoke(app, [*args, "--absolute-targets"])
    assert "/km Pace" in absolute.output


def test_push_warns_when_thresholds_differ(
    plan_path: Path, profile_path: Path, fake_api: FakeApi
) -> None:
    args = ["push", str(plan_path), "-p", str(profile_path), *TODAY]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "thresholds differ" in result.output
    assert "bike FTP (W): profile 260, intervals.icu 265" in result.output
    count = sum(len(week.sessions) for week in load_plan(plan_path).weeks)
    assert f"pushed {count} workout(s), removed 0 stale event(s)" in result.output
    assert len(fake_api.events) == count
    again = runner.invoke(app, args)
    assert again.exit_code == 0 and len(fake_api.events) == count  # idempotent


def test_push_reports_api_errors(plan_path: Path, profile_path: Path, fake_api: FakeApi) -> None:
    fake_api.failures = [401]
    result = runner.invoke(app, ["push", str(plan_path), "-p", str(profile_path), *TODAY])
    assert result.exit_code == 1
    assert "authentication failed" in result.output


def test_push_needs_an_api_key(
    plan_path: Path, profile_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    result = runner.invoke(app, ["push", str(plan_path), "-p", str(profile_path), *TODAY])
    assert result.exit_code == 1
    assert "INTERVALS_API_KEY is not set" in result.output


def test_clear(plan_path: Path, profile_path: Path, fake_api: FakeApi) -> None:
    runner.invoke(app, ["push", str(plan_path), "-p", str(profile_path), *TODAY])
    fake_api.events[1] = {"id": 1, "start_date_local": "2026-11-18T00:00:00", "name": "Mine"}
    count = len(fake_api.events) - 1
    dry = runner.invoke(app, ["clear", str(plan_path), "--dry-run"])
    assert f"{count} event(s) would be removed" in dry.output
    assert len(fake_api.events) == count + 1
    declined = runner.invoke(app, ["clear", str(plan_path)], input="n\n")
    assert declined.exit_code == 1 and len(fake_api.events) == count + 1
    done = runner.invoke(app, ["clear", str(plan_path), "--yes"])
    assert done.exit_code == 0 and f"removed {count} event(s)" in done.output
    assert list(fake_api.events) == [1]
    empty = runner.invoke(
        app, ["clear", "--all-plans", "--from", "2026-11-01", "--to", "2027-12-31"]
    )
    assert "no event created by this tool" in empty.output
    assert runner.invoke(app, ["clear"]).exit_code == 1
    assert runner.invoke(app, ["clear", "--all-plans"]).exit_code == 1


def test_profile_sync_intervals(tmp_path: Path, fake_api: FakeApi) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    args = ["profile", "sync-intervals", "-p", str(profile), *TODAY]
    declined = runner.invoke(app, args, input="n\n")
    assert "bike FTP (W): profile 260, intervals.icu 265" in declined.output
    assert "profile left unchanged" in declined.output
    assert profile.read_text(encoding="utf-8") == EXAMPLE.read_text(encoding="utf-8")
    accepted = runner.invoke(app, [*args, "--yes"])
    assert accepted.exit_code == 0, accepted.output
    updated = load_profile(profile)
    assert updated.thresholds.bike is not None and updated.thresholds.bike.ftp_w == 265
    assert updated.thresholds.run is not None and updated.thresholds.run.threshold_pace == 250
    assert "# [required for bike sessions]" in profile.read_text(encoding="utf-8")  # comments kept
    assert "profile thresholds match intervals.icu" in runner.invoke(app, args).output
