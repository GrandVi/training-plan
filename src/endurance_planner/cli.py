"""Command-line interface. Thin layer: parsing, file I/O and printing only."""

import datetime as dt
import json
from pathlib import Path
from typing import Annotated

import typer

from endurance_planner import services
from endurance_planner.amend import AmendError
from endurance_planner.config import ProfileError, load_profile
from endurance_planner.export.intervals_icu import (
    IntervalsClient,
    IntervalsError,
    clear_events,
    remote_thresholds,
    threshold_diffs,
    update_yaml_scalars,
)
from endurance_planner.export.json_io import load_plan, save_plan
from endurance_planner.export.markdown import render_markdown
from endurance_planner.models.enums import BikeActivity, GoalType, Intent, Scope, Severity, Sport
from endurance_planner.models.issues import Issue
from endurance_planner.models.plan import Plan
from endurance_planner.models.profile import Profile
from endurance_planner.periodization import PlanningError
from endurance_planner.profile_tools import dump_profile, starter_profile
from endurance_planner.rules import DEFAULT_SCOPE
from endurance_planner.schemas import plan_json_schema, profile_json_schema
from endurance_planner.settings import environment, intervals_settings
from endurance_planner.swap import SwapError
from endurance_planner.thresholds import resolve_thresholds
from endurance_planner.validation import PlanValidationError

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


AthleteOption = Annotated[
    str | None,
    typer.Option(
        "--athlete-id", help="intervals.icu athlete id (default: INTERVALS_ATHLETE_ID or 0)."
    ),
]
VerboseOption = Annotated[
    bool, typer.Option("--verbose", "-v", help="Log every request and response.")
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


def _json(data: object) -> str:
    return json.dumps(data, indent=2) + "\n"


OutOption = Annotated[Path | None, typer.Option("--out", "-o", help="Write to a file.")]
InPlaceOption = Annotated[
    Path | None, typer.Option("--out", "-o", help="Output file (default: in place).")
]
ScopeOption = Annotated[
    Scope,
    typer.Option(help="session: only this session; week: also its week; rest: later weeks too."),
]
DryRunOption = Annotated[bool, typer.Option("--dry-run", help="Print the changes, write nothing.")]
ForceOption = Annotated[
    bool, typer.Option(help="Write the plan even if it breaks a rule (the errors are listed).")
]
ReasonOption = Annotated[str, typer.Option(help="Why (kept in the amendment log).")]


def _changes(
    outcome: services.Outcome,
    plan: Path,
    out: Path | None,
    *,
    refused: str = "not applied: the plan would break at least one rule (use --force)",
) -> None:
    """Print the changes and issues, then write the plan unless refused or a dry run."""
    result = outcome.result
    for line in services.change_lines(result):
        typer.echo(line)
    _print_issues(result.issues)
    if result.dry_run:
        typer.echo("dry run: nothing written", err=True)
        return
    if outcome.stored is None:
        raise _fail(refused)
    save_plan(outcome.stored, out or plan)
    typer.echo(f"wrote {out or plan}", err=True)


def make_client(athlete_id: str | None, verbose: bool) -> IntervalsClient:
    """intervals.icu client from the environment or a .env file (the key is never stored)."""
    settings = intervals_settings(environment(), athlete_id)
    if not settings.api_key:
        raise _fail("INTERVALS_API_KEY is not set (environment variable or .env file)")
    try:
        from endurance_planner.http_transport import HttpxTransport
    except ImportError as exc:
        raise _fail(
            'intervals.icu needs the http extra: pip install "endurance-planner[http]"'
        ) from exc
    log = (lambda message: typer.echo(message, err=True)) if verbose else None
    return IntervalsClient(
        settings.api_key, settings.athlete_id, transport=HttpxTransport(), log=log
    )


# ---------------------------------------------------------------------- profile
@profile_app.command("validate")
def profile_validate(
    profile: ProfileOption = Path("profile.yaml"), today: TodayOption = None
) -> None:
    """Check the profile and print the resolved zone tables."""
    typer.echo(services.profile_text(_profile(profile), _date(today)), nl=False)


@profile_app.command("schema")
def profile_schema(out: OutOption = None) -> None:
    """Print the JSON Schema of profile.yaml (for editor validation and completion)."""
    _write(_json(profile_json_schema()), out)


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
        outcome = services.generate_plan(athlete, _date(today), _date(start) if start else None)
    except (PlanningError, PlanValidationError) as exc:
        raise _fail(str(exc)) from exc
    plan = outcome.candidate
    _print_issues(outcome.result.issues)
    save_plan(plan, out)
    typer.echo(f"wrote {out}: {outcome.result.message}", err=True)
    if markdown is not None:
        _write(render_markdown(plan, athlete), markdown)


@plan_app.command("schema")
def plan_schema(out: OutOption = None) -> None:
    """Print the JSON Schema of plan.json."""
    _write(_json(plan_json_schema()), out)


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
    try:
        text = services.render_plan(loaded, athlete, fmt, weeks=week, details=details)
    except services.UnknownFormat as exc:
        raise _fail(str(exc)) from exc
    _write(text, out)


@plan_app.command("validate")
def plan_validate(
    plan: PlanArgument, profile: ProfileOption = Path("profile.yaml"), today: TodayOption = None
) -> None:
    """Check a plan against every rule; exit code 1 if there are errors."""
    loaded = _plan(plan)
    report = services.validate_plan(loaded, _profile(profile), _date(today, loaded.created))
    _print_issues(report.issues)
    typer.echo(f"{report.errors} error(s), {report.warnings} warning(s)")
    if report.errors:
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
    loaded = _plan(plan)
    outcome = services.retarget(
        loaded, _profile(profile), _date(today), expected_revision=loaded.revision
    )
    _print_issues(outcome.result.issues)
    save_plan(outcome.candidate, out or plan)
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
    """Convert one session to the other sport, preserving intent and impact-weighted load.

    Shortcut for `plan amend --sport <other sport> --scope session`.
    """
    loaded, athlete = _plan(plan), _profile(profile)
    try:
        outcome = services.swap(
            loaded,
            athlete,
            _date(today),
            _date(date),
            expected_revision=loaded.revision,
            force=force,
        )
    except (AmendError, SwapError) as exc:
        raise _fail(str(exc)) from exc
    _print_issues(outcome.result.issues)
    if outcome.stored is None:
        raise _fail("swap not applied: it breaks at least one rule (use --force to apply anyway)")
    save_plan(outcome.stored, out or plan)
    typer.echo(outcome.result.message, err=True)


@plan_app.command("amend")
def plan_amend(
    plan: PlanArgument,
    date: Annotated[str, typer.Option(help="Date of the session to change (YYYY-MM-DD).")],
    minutes: Annotated[int | None, typer.Option(min=1, help="New duration in minutes.")] = None,
    sport: Annotated[Sport | None, typer.Option(help="Do it in this sport.")] = None,
    activity: Annotated[
        BikeActivity | None, typer.Option(help="Bike type: road, mtb or trainer.")
    ] = None,
    intent: Annotated[
        Intent | None, typer.Option(help="Make it easier: endurance or recovery.")
    ] = None,
    skip: Annotated[bool, typer.Option(help="Skip the session (nothing else combines).")] = False,
    move_to: Annotated[
        str | None, typer.Option(help="Move it to this day of the same week.")
    ] = None,
    scope: ScopeOption = DEFAULT_SCOPE,
    dry_run: DryRunOption = False,
    reason: ReasonOption = "",
    force: ForceOption = False,
    profile: ProfileOption = Path("profile.yaml"),
    out: InPlaceOption = None,
    today: TodayOption = None,
) -> None:
    """Change one session (shorter, other sport or bike, easier, skipped, moved); the rest
    of the plan adapts according to --scope. The session is pinned and the change logged."""
    loaded, athlete = _plan(plan), _profile(profile)
    request = services.AmendRequest(
        date=_date(date),
        minutes=minutes,
        sport=sport,
        activity=activity,
        intent=intent,
        skip=skip,
        move_to=_date(move_to) if move_to else None,
        scope=scope,
        reason=reason,
    )
    try:
        outcome = services.amend(
            loaded,
            athlete,
            _date(today),
            request,
            expected_revision=loaded.revision,
            dry_run=dry_run,
            force=force,
        )
    except (AmendError, SwapError, PlanningError) as exc:
        raise _fail(str(exc)) from exc
    if outcome.result.message:
        typer.echo(outcome.result.message, err=True)
    _changes(outcome, plan, out)


def _day_minutes(value: str | None) -> dict[Sport, int] | None:
    """'run=0,bike=60' -> {run: 0, bike: 60}."""
    if value is None:
        return None
    minutes: dict[Sport, int] = {}
    for part in value.split(","):
        name, _, number = part.partition("=")
        try:
            minutes[Sport(name.strip())] = int(number)
        except ValueError as exc:
            raise _fail(
                f"--minutes: expected sport=minutes pairs like run=0,bike=60, got {value!r}"
            ) from exc
    return minutes


@plan_app.command("unavailable")
def plan_unavailable(
    plan: PlanArgument,
    date_from: Annotated[str, typer.Option("--from", help="First day (YYYY-MM-DD).")],
    date_to: Annotated[str | None, typer.Option("--to", help="Last day (default: --from).")] = None,
    minutes: Annotated[
        str | None,
        typer.Option(help="Minutes per sport, e.g. run=0,bike=60 (default: no training)."),
    ] = None,
    activity: Annotated[
        BikeActivity | None, typer.Option(help="Force the bike type: road, mtb or trainer.")
    ] = None,
    scope: ScopeOption = DEFAULT_SCOPE,
    dry_run: DryRunOption = False,
    reason: ReasonOption = "",
    force: ForceOption = False,
    profile: ProfileOption = Path("profile.yaml"),
    out: InPlaceOption = None,
    today: TodayOption = None,
) -> None:
    """Make days unavailable or limited (same rules as availability.exceptions) and adapt."""
    loaded, athlete = _plan(plan), _profile(profile)
    request = services.UnavailableRequest(
        start=_date(date_from),
        end=_date(date_to) if date_to else None,
        minutes=_day_minutes(minutes),
        activity=activity,
        scope=scope,
        reason=reason,
    )
    try:
        outcome = services.declare_unavailable(
            loaded,
            athlete,
            _date(today),
            request,
            expected_revision=loaded.revision,
            dry_run=dry_run,
            force=force,
        )
    except (AmendError, PlanningError) as exc:
        raise _fail(str(exc)) from exc
    if outcome.result.message:
        typer.echo(outcome.result.message, err=True)
    _changes(outcome, plan, out)


@plan_app.command("replan")
def plan_replan(
    plan: PlanArgument,
    dry_run: DryRunOption = False,
    force: ForceOption = False,
    profile: ProfileOption = Path("profile.yaml"),
    out: InPlaceOption = None,
    today: TodayOption = None,
) -> None:
    """Rebuild the plan from today after editing profile.yaml; past and pinned sessions stay."""
    loaded, athlete = _plan(plan), _profile(profile)
    try:
        outcome = services.replan(
            loaded,
            athlete,
            _date(today),
            expected_revision=loaded.revision,
            dry_run=dry_run,
            force=force,
        )
    except PlanningError as exc:
        raise _fail(str(exc)) from exc
    _changes(outcome, plan, out)


@plan_app.command("amendments")
def plan_amendments(
    plan: PlanArgument,
    undo: Annotated[
        str | None, typer.Option(help="Remove this amendment (e.g. a2) and replan.")
    ] = None,
    dry_run: DryRunOption = False,
    force: ForceOption = False,
    profile: ProfileOption = Path("profile.yaml"),
    out: InPlaceOption = None,
    today: TodayOption = None,
) -> None:
    """List the plan's amendments, or undo one."""
    loaded = _plan(plan)
    if undo is None:
        for line in [view.line for view in services.list_amendments(loaded)] or ["no amendment"]:
            typer.echo(line)
        return
    try:
        outcome = services.undo_amendment(
            loaded,
            _profile(profile),
            _date(today),
            undo,
            expected_revision=loaded.revision,
            dry_run=dry_run,
            force=force,
        )
    except (AmendError, PlanningError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(outcome.result.message, err=True)
    _changes(outcome, plan, out)


@profile_app.command("sync-intervals")
def profile_sync_intervals(
    profile: ProfileOption = Path("profile.yaml"),
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without asking.")] = False,
    athlete_id: AthleteOption = None,
    verbose: VerboseOption = False,
    today: TodayOption = None,
) -> None:
    """Compare thresholds with intervals.icu and offer to update the profile."""
    athlete = _profile(profile)
    local, _ = resolve_thresholds(athlete, _date(today))
    try:
        remote = remote_thresholds(make_client(athlete_id, verbose).athlete())
    except IntervalsError as exc:
        raise _fail(str(exc)) from exc
    diffs = threshold_diffs(local, remote)
    if not diffs:
        typer.echo("profile thresholds match intervals.icu")
        return
    for diff in diffs:
        typer.echo(f"  {diff.label}")
    if not (yes or typer.confirm(f"Write the intervals.icu values to {profile}?", default=False)):
        typer.echo("profile left unchanged")
        return
    text, missing = update_yaml_scalars(
        profile.read_text(encoding="utf-8"), {diff.path: diff.remote for diff in diffs}
    )
    profile.write_text(text, encoding="utf-8")
    typer.echo(f"updated {profile}")
    for path in missing:
        typer.echo(f"  {path} is not in the file: add it by hand")


# ---------------------------------------------------------------------- intervals.icu
@app.command()
def push(
    plan: PlanArgument,
    profile: ProfileOption = Path("profile.yaml"),
    dry_run: Annotated[bool, typer.Option(help="Print the payloads, no network.")] = False,
    date_from: Annotated[
        str | None, typer.Option("--from", help="First date to push (default: today).")
    ] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Last date to push.")] = None,
    absolute_targets: Annotated[
        bool, typer.Option(help="Push watts and paces instead of % of threshold.")
    ] = False,
    prune: Annotated[
        bool, typer.Option(help="Delete this plan's events that are no longer in the plan.")
    ] = True,
    only_changed: Annotated[
        bool,
        typer.Option(
            "--only-changed/--all",
            help="Write only the events that differ from the calendar (default), or all.",
        ),
    ] = True,
    athlete_id: AthleteOption = None,
    verbose: VerboseOption = False,
    today: TodayOption = None,
) -> None:
    """Push the plan to the intervals.icu calendar (idempotent: re-running updates)."""
    loaded = _plan(plan)
    request = services.PushRequest(
        dry_run=dry_run,
        start=_date(date_from) if date_from else _date(today),
        end=_date(date_to) if date_to else None,
        absolute_targets=absolute_targets,
        prune=prune,
        only_changed=only_changed,
    )
    if dry_run:
        report = services.push_plan(loaded, None, _date(today), request)
        typer.echo(json.dumps(report.payloads, indent=2))
        return
    athlete = _profile(profile)
    try:
        report = services.push_plan(
            loaded, athlete, _date(today), request, make_client(athlete_id, verbose)
        )
    except IntervalsError as exc:
        raise _fail(str(exc)) from exc
    if report.threshold_diffs and not absolute_targets:
        typer.secho(
            "warning: thresholds differ between the profile and intervals.icu; relative "
            "targets are resolved by intervals.icu with ITS settings:",
            fg=typer.colors.YELLOW,
            err=True,
        )
        for label in report.threshold_diffs:
            typer.secho(f"  {label}", fg=typer.colors.YELLOW, err=True)
    unchanged = f", {report.unchanged} unchanged" if report.unchanged else ""
    typer.echo(
        f"pushed {report.upserted} workout(s), removed {report.pruned} stale event(s){unchanged}"
    )


@app.command()
def clear(
    plan: Annotated[Path | None, typer.Argument(help="Plan whose events are removed.")] = None,
    all_plans: Annotated[
        bool, typer.Option(help="Remove the events of every plan pushed by this tool.")
    ] = False,
    date_from: Annotated[str | None, typer.Option("--from", help="First date.")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Last date.")] = None,
    dry_run: Annotated[bool, typer.Option(help="List the events, delete nothing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Delete without asking.")] = False,
    athlete_id: AthleteOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Remove from intervals.icu the events created by this tool, and only those."""
    plan_id = None
    oldest = _date(date_from) if date_from else None
    newest = _date(date_to) if date_to else None
    if plan is not None and not all_plans:
        loaded = _plan(plan)
        plan_id = loaded.id
        oldest = oldest or loaded.weeks[0].start
        newest = newest or loaded.weeks[-1].start + dt.timedelta(days=6)
    elif not all_plans:
        raise _fail("give a plan file, or --all-plans with --from and --to")
    if oldest is None or newest is None:
        raise _fail("--all-plans needs --from and --to")
    try:
        client = make_client(athlete_id, verbose)
        events = clear_events(client, oldest, newest, plan_id, dry_run=True)
        for event in events:
            typer.echo(f"  {str(event.get('start_date_local'))[:10]} {event.get('name')}")
        if not events:
            typer.echo("no event created by this tool in that window")
            return
        if dry_run:
            typer.echo(f"{len(events)} event(s) would be removed")
            return
        if not (yes or typer.confirm(f"Remove these {len(events)} event(s)?", default=False)):
            raise typer.Exit(code=1)
        client.delete_events([int(event["id"]) for event in events])
    except IntervalsError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"removed {len(events)} event(s)")
