"""Command-line interface. Thin layer: parsing, file I/O and printing only."""

import datetime as dt
from pathlib import Path
from typing import Annotated

import typer

from endurance_planner.config import ProfileError, load_profile
from endurance_planner.export.json_io import load_plan, save_plan
from endurance_planner.export.markdown import fitness_csv, render_markdown, weekly_csv
from endurance_planner.models.enums import GoalType, Severity
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile
from endurance_planner.periodization import PlanningError
from endurance_planner.planner import generate_plan, retarget_plan
from endurance_planner.profile_tools import dump_profile, starter_profile, zone_report
from endurance_planner.swap import SwapError, swap_session
from endurance_planner.thresholds import resolve_thresholds
from endurance_planner.validation import PlanValidationError, validate_plan

app = typer.Typer(no_args_is_help=True, help="Multi-sport training plan generator.")
profile_app = typer.Typer(no_args_is_help=True, help="Create and check the athlete profile.")
plan_app = typer.Typer(no_args_is_help=True, help="Generate, inspect and edit plans.")
app.add_typer(profile_app, name="profile")
app.add_typer(plan_app, name="plan")

ProfileOption = Annotated[Path, typer.Option("--profile", "-p", help="Athlete profile (YAML).")]
PlanArgument = Annotated[Path, typer.Argument(help="Plan file (JSON).")]
TodayOption = Annotated[
    str | None, typer.Option("--today", help="Override today's date (YYYY-MM-DD).", hidden=True)
]


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _date(value: str | None, default: dt.date | None = None) -> dt.date:
    if value is None:
        return default or dt.date.today()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise _fail(f"invalid date '{value}', expected YYYY-MM-DD") from exc


def _profile(path: Path) -> Profile:
    try:
        return load_profile(path)
    except (ProfileError, OSError) as exc:
        raise _fail(str(exc)) from exc


def _plan(path: Path) -> Plan:
    try:
        return load_plan(path)
    except (OSError, ValueError) as exc:
        raise _fail(f"{path}: cannot read plan: {exc}") from exc


def _print_issues(issues: list[Issue]) -> None:
    for issue in issues:
        color = typer.colors.RED if issue.severity is Severity.ERROR else typer.colors.YELLOW
        typer.secho(str(issue), fg=color, err=True)


def _write(text: str, out: Path | None) -> None:
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}", err=True)


# ---------------------------------------------------------------------- profile
@profile_app.command("validate")
def profile_validate(
    profile: ProfileOption = Path("profile.yaml"), today: TodayOption = None
) -> None:
    """Check the profile and print the resolved zone tables."""
    typer.echo(zone_report(_profile(profile), _date(today)), nl=False)


@profile_app.command("init")
def profile_init(
    out: Annotated[Path, typer.Option("--out", "-o", help="File to write.")] = Path("profile.yaml"),
) -> None:
    """Interactive wizard: asks for the required fields and writes a profile."""
    if out.exists() and not typer.confirm(f"{out} exists. Overwrite?", default=False):
        raise typer.Exit(code=1)
    name = typer.prompt("Name")
    ftp = typer.prompt("Bike FTP in watts (0 if you do not ride)", type=float, default=0.0)
    pace = typer.prompt("Run threshold pace m:ss per km (empty if you do not run)", default="")
    bike_hours = typer.prompt("Current bike hours per week", type=float, default=0.0)
    run_hours = typer.prompt("Current run hours per week", type=float, default=0.0)
    days = typer.prompt("Training days per week", type=int, default=4)
    default_budget = max(round(bike_hours + run_hours), 3)
    budget = typer.prompt("Weekly hours budget (hard cap)", type=float, default=default_budget)
    goal_name = typer.prompt("Goal name")
    goal_date = _date(typer.prompt("Goal date (YYYY-MM-DD)"))
    types = ", ".join(goal_type.value for goal_type in GoalType)
    goal_type = typer.prompt(f"Goal type ({types})", default=GoalType.RUN_10K.value)
    data = starter_profile(
        name=name,
        ftp_w=ftp or None,
        threshold_pace=pace.strip() or None,
        bike_hours=bike_hours,
        run_hours=run_hours,
        days_per_week=days,
        weekly_hours_budget=budget,
        goal_name=goal_name,
        goal_date=goal_date,
        goal_type=goal_type,
    )
    try:
        Profile.model_validate(data)
    except ValueError as exc:
        raise _fail(f"these answers do not make a valid profile:\n{exc}") from exc
    out.write_text(dump_profile(data), encoding="utf-8")
    typer.echo(
        f"wrote {out}; edit availability.weekdays to match your week, then run "
        "`endurance-planner profile validate`"
    )


# ---------------------------------------------------------------------- plan
@plan_app.command("generate")
def plan_generate(
    profile: ProfileOption = Path("profile.yaml"),
    out: Annotated[Path, typer.Option("--out", "-o", help="Plan file to write.")] = Path(
        "plan.json"
    ),
    markdown: Annotated[
        Path | None, typer.Option("--markdown", "-m", help="Also write a Markdown summary.")
    ] = None,
    start: Annotated[
        str | None, typer.Option(help="First Monday of the plan (default: next Monday).")
    ] = None,
    today: TodayOption = None,
) -> None:
    """Generate a plan from the profile and write it as JSON."""
    athlete = _profile(profile)
    try:
        plan = generate_plan(athlete, _date(today), _date(start) if start else None)
    except (PlanningError, PlanValidationError) as exc:
        raise _fail(str(exc)) from exc
    _print_issues(plan.warnings)
    save_plan(plan, out)
    typer.echo(f"wrote {out}: {len(plan.weeks)} weeks from {plan.weeks[0].start}", err=True)
    if markdown is not None:
        _write(render_markdown(plan, athlete), markdown)


@plan_app.command("show")
def plan_show(
    plan: PlanArgument,
    profile: ProfileOption = Path("profile.yaml"),
    week: Annotated[list[int] | None, typer.Option(help="Week number(s) to detail.")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="md, csv or fitness-csv.")] = "md",
    details: Annotated[bool, typer.Option(help="Include session details (md).")] = True,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write to a file.")] = None,
) -> None:
    """Print a plan as Markdown, a weekly CSV, or a daily CTL/ATL/TSB CSV."""
    loaded, athlete = _plan(plan), _profile(profile)
    if fmt == "md":
        _write(render_markdown(loaded, athlete, weeks=week, details=details), out)
    elif fmt == "csv":
        _write(weekly_csv(loaded, athlete), out)
    elif fmt == "fitness-csv":
        _write(fitness_csv(loaded, athlete), out)
    else:
        raise _fail(f"unknown format '{fmt}' (md, csv, fitness-csv)")


@plan_app.command("validate")
def plan_validate(
    plan: PlanArgument, profile: ProfileOption = Path("profile.yaml"), today: TodayOption = None
) -> None:
    """Check a plan against every rule; exit code 1 if there are errors."""
    loaded = _plan(plan)
    issues = validate_plan(loaded, _profile(profile), _date(today, loaded.created))
    _print_issues(issues)
    errors = sum(issue.severity is Severity.ERROR for issue in issues)
    typer.echo(f"{errors} error(s), {len(issues) - errors} warning(s)")
    if errors:
        raise typer.Exit(code=1)


@plan_app.command("retarget")
def plan_retarget(
    plan: PlanArgument,
    profile: ProfileOption = Path("profile.yaml"),
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Output file (default: in place).")
    ] = None,
    today: TodayOption = None,
) -> None:
    """Recompute absolute targets after a threshold change; structure and dates are kept."""
    updated, issues = retarget_plan(_plan(plan), _profile(profile), _date(today))
    _print_issues(issues)
    save_plan(updated, out or plan)
    typer.echo(f"wrote {out or plan}", err=True)


@plan_app.command("swap")
def plan_swap(
    plan: PlanArgument,
    date: Annotated[str, typer.Option(help="Date of the session to swap (YYYY-MM-DD).")],
    profile: ProfileOption = Path("profile.yaml"),
    force: Annotated[
        bool, typer.Option(help="Write the plan even if the swap breaks a rule.")
    ] = False,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Output file (default: in place).")
    ] = None,
    today: TodayOption = None,
) -> None:
    """Convert one session to the other sport, preserving intent and impact-weighted load."""
    loaded, athlete = _plan(plan), _profile(profile)
    thresholds, _ = resolve_thresholds(athlete, _date(today))
    try:
        swapped, issues = swap_session(loaded, _date(date), athlete, thresholds)
    except SwapError as exc:
        raise _fail(str(exc)) from exc
    _print_issues(issues)
    if any(issue.severity is Severity.ERROR for issue in issues) and not force:
        raise _fail("swap not applied: it breaks at least one rule (use --force to apply anyway)")
    save_plan(swapped, out or plan)
    session = next(s for week in swapped.weeks for s in week.sessions if s.date == _date(date))
    typer.echo(f"{session.date}: now a {session.sport.value} session, {session.name}", err=True)
