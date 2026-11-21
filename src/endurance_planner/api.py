"""Adapter: the HTTP API (FastAPI, extra `api`) on a data directory.

Thin layer over services.PlannerService: parsing, authentication, error mapping. Every
route but /health (and the static web app, which holds no data) needs the bearer token
from PLANNER_TOKEN. intervals.icu credentials are read from the server environment only and
never returned or logged.
"""

import datetime as dt
import hmac
import logging
import mimetypes
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from endurance_planner import __version__, services
from endurance_planner.amend import AmendError, NoSession
from endurance_planner.export.intervals_icu import IntervalsClient, IntervalsError
from endurance_planner.models.enums import BikeActivity, Intent, Scope, Sport
from endurance_planner.periodization import PlanningError
from endurance_planner.replan import ReplanError
from endurance_planner.rules import DEFAULT_SCOPE
from endurance_planner.serialization import ProfileError
from endurance_planner.services import (
    AmendmentView,
    MutationResult,
    PlannerService,
    PlanOverview,
    ProfileSummary,
    PushReport,
    WeekView,
)
from endurance_planner.settings import intervals_settings
from endurance_planner.storage import LOCK_TIMEOUT_S, FileStorage
from endurance_planner.swap import SwapError
from endurance_planner.validation import PlanValidationError

# Same content types on every Python version and system (the web app is ES modules).
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")

LOCAL_HOST = "127.0.0.1"
MIN_TOKEN_LENGTH = 16
WEB_DIR = Path(__file__).parent / "web"
log = logging.getLogger("endurance_planner.api")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class ServerConfigError(ValueError):
    """The server must not start with these settings; the message says what to change."""


def check_server_settings(token: str | None, host: str, insecure_local: bool) -> None:
    """A token is required, unless --insecure-local on 127.0.0.1 only."""
    if insecure_local:
        if host != LOCAL_HOST:
            raise ServerConfigError(f"--insecure-local only works with --host {LOCAL_HOST}")
        return
    if not token:
        raise ServerConfigError(
            "PLANNER_TOKEN is not set: generate one (for example "
            '`python -c "import secrets; print(secrets.token_urlsafe(32))"`) and put it in '
            f"the server environment, or use --insecure-local with --host {LOCAL_HOST}"
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise ServerConfigError(
            f"PLANNER_TOKEN is too short (at least {MIN_TOKEN_LENGTH} characters)"
        )


# ---------------------------------------------------------------------- request bodies
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MutationBody(_Body):
    expected_revision: int = Field(ge=0, description="Revision the change is based on")
    dry_run: bool = Field(False, description="Return the changes, store nothing")
    force: bool = Field(False, description="Store the change even if it breaks a rule")


class GenerateBody(MutationBody):
    """expected_revision is 0 when there is no plan yet; force has no effect (a generated
    plan always passes every rule, or generation fails)."""

    start: dt.date | None = Field(None, description="First Monday (default: next Monday)")


class AmendBody(MutationBody):
    date: dt.date
    minutes: int | None = Field(None, gt=0)
    sport: Sport | None = None
    activity: BikeActivity | None = None
    intent: Intent | None = None
    skip: bool = False
    move_to: dt.date | None = None
    scope: Scope = DEFAULT_SCOPE
    reason: str = ""


class UnavailableBody(MutationBody):
    start: dt.date = Field(alias="from")
    end: dt.date | None = Field(None, alias="to")
    minutes: dict[Sport, int] | None = None
    activity: BikeActivity | None = None
    scope: Scope = DEFAULT_SCOPE
    reason: str = ""


class PushBody(_Body):
    dry_run: bool = Field(True, description="Return the payloads, send nothing (default)")
    start: dt.date | None = Field(None, alias="from")
    end: dt.date | None = Field(None, alias="to")
    absolute_targets: bool = False


class Health(BaseModel):
    status: str
    version: str


# ---------------------------------------------------------------------- errors
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        self.status, self.code, self.message, self.extra = status, code, message, extra
        super().__init__(message)


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    if status == 503:
        headers = {"Retry-After": "1"}
    body = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(body, status_code=status, headers=headers)


def _issues(issues: list[Any]) -> list[dict[str, Any]]:
    return [issue.model_dump(mode="json") for issue in issues]


def _domain_error(exc: Exception) -> JSONResponse:
    """Library errors -> 4xx/5xx with the library's message (never a stack trace)."""
    match exc:
        case ApiError():
            return _error(exc.status, exc.code, exc.message, **exc.extra)
        case services.RevisionConflict():
            return _error(
                409,
                "revision_conflict",
                str(exc),
                current_revision=exc.current,
                expected_revision=exc.expected,
            )
        case services.PlanNotFound():
            return _error(404, "no_plan", str(exc))
        case services.UnknownWeek():
            return _error(404, "unknown_week", str(exc))
        case NoSession():
            return _error(404, "unknown_date", str(exc))
        case ReplanError():
            return _error(422, exc.code, str(exc))
        case PlanValidationError():
            return _error(422, "plan_invalid", str(exc).splitlines()[0], issues=_issues(exc.issues))
        case AmendError():
            return _error(422, "amend_refused", str(exc))
        case SwapError():
            return _error(422, "swap_refused", str(exc))
        case PlanningError():
            return _error(422, "planning_error", str(exc))
        case ProfileError():
            return _error(503, "profile_invalid", str(exc))
        case services.StorageBusy():
            return _error(503, "busy", str(exc))
        case IntervalsError():
            return _error(502, "intervals_error", str(exc))
    log.exception("internal error")
    return _error(500, "internal_error", "internal error (see the server log)")


def _refused(result: MutationResult) -> MutationResult:
    """A change that breaks a rule is stored only with force: 422 with its issues."""
    if result.refused and not result.dry_run:
        raise ApiError(
            422,
            "rules_broken",
            "not applied: the plan would break at least one rule (send force: true to apply)",
            issues=_issues(result.issues),
            changes=[change.model_dump(mode="json") for change in result.changes],
        )
    return result


# ---------------------------------------------------------------------- application
def default_client_factory(env: Mapping[str, str]) -> Callable[[], IntervalsClient] | None:
    """intervals.icu client from INTERVALS_API_KEY / INTERVALS_ATHLETE_ID (server only)."""
    settings = intervals_settings(env)
    if not settings.api_key:
        return None
    api_key, athlete_id = settings.api_key, settings.athlete_id

    def factory() -> IntervalsClient:
        from endurance_planner.http_transport import HttpxTransport

        return IntervalsClient(api_key, athlete_id, transport=HttpxTransport())

    return factory


def create_app(
    data_dir: Path,
    *,
    token: str | None,
    insecure_local: bool = False,
    today: Callable[[], dt.date] = dt.date.today,
    client_factory: Callable[[], IntervalsClient] | None = None,
    env: Mapping[str, str] | None = None,
    web_dir: Path | None = WEB_DIR,
    lock_timeout_s: float = LOCK_TIMEOUT_S,
) -> FastAPI:
    """The API on `data_dir`. Without `token`, `insecure_local` must be set (local only).
    `client_factory` defaults to the intervals.icu credentials of `env` (os.environ)."""
    if not token and not insecure_local:
        raise ServerConfigError("a token is required (or insecure_local for 127.0.0.1)")
    expected = token.encode() if token else None
    factory = client_factory or default_client_factory(os.environ if env is None else env)
    service = PlannerService(FileStorage(data_dir, lock_timeout_s=lock_timeout_s), factory)
    app = FastAPI(
        title="endurance-planner",
        version=__version__,
        description="Training plan API. Send `Authorization: Bearer <PLANNER_TOKEN>`.",
        docs_url=None,  # Swagger UI loads from a CDN: use /openapi.json with the token
        redoc_url=None,
        openapi_url=None,
    )

    def authorize(request: Request) -> None:
        if expected is None:
            return
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), expected):
            raise ApiError(401, "unauthorized", "missing or invalid token")

    Auth = Annotated[None, Depends(authorize)]

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith(("/plan", "/profile", "/openapi")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        return _domain_error(exc)

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _domain_error(exc)

    for error in (
        services.RevisionConflict,
        services.PlanNotFound,
        services.UnknownWeek,
        services.StorageBusy,
        AmendError,
        SwapError,
        PlanningError,
        PlanValidationError,
        ProfileError,
        IntervalsError,
    ):
        app.add_exception_handler(error, on_error)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return _error(422, "invalid_request", "the request is not valid", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def on_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, "http_error", str(exc.detail))

    # ------------------------------------------------------------------ routes
    @app.get("/health", response_model=Health, tags=["meta"])
    def health() -> Health:
        """Open route: the server is up."""
        return Health(status="ok", version=__version__)

    @app.get("/openapi.json", include_in_schema=False)
    def openapi(_: Auth) -> dict[str, Any]:
        return app.openapi()

    @app.get("/profile/summary", response_model=ProfileSummary, tags=["profile"])
    def profile_summary(_: Auth) -> ProfileSummary:
        """Thresholds, zones and warnings of the server's profile.yaml (read-only)."""
        return service.profile_summary(today())

    @app.get("/plan", response_model=PlanOverview, tags=["plan"])
    def plan(_: Auth) -> PlanOverview:
        """The current plan: weeks, goals, warnings, revision."""
        return service.overview(today())

    @app.get("/plan/weeks/{index}", response_model=WeekView, tags=["plan"])
    def week(index: int, _: Auth) -> WeekView:
        """One week (1-based) with its sessions, steps and absolute targets."""
        return service.week(index)

    @app.post("/plan/generate", response_model=MutationResult, tags=["plan"])
    def generate(body: GenerateBody, _: Auth) -> MutationResult:
        """A new plan from the profile (replaces the current one; its revision goes on)."""
        return service.generate(
            today(), body.start, expected_revision=body.expected_revision, dry_run=body.dry_run
        )

    @app.post("/plan/amend", response_model=MutationResult, tags=["changes"])
    def amend(body: AmendBody, _: Auth) -> MutationResult:
        """Change one session: shorter, other sport or bike, easier, skipped or moved."""
        request = services.AmendRequest.model_validate(
            body.model_dump(exclude={"expected_revision", "dry_run", "force"})
        )
        return _refused(
            service.amend(
                today(),
                request,
                expected_revision=body.expected_revision,
                dry_run=body.dry_run,
                force=body.force,
            )
        )

    @app.post("/plan/unavailable", response_model=MutationResult, tags=["changes"])
    def unavailable(body: UnavailableBody, _: Auth) -> MutationResult:
        """A date range without training, or with limited minutes per sport."""
        request = services.UnavailableRequest.model_validate(
            body.model_dump(exclude={"expected_revision", "dry_run", "force"}, by_alias=True)
        )
        return _refused(
            service.declare_unavailable(
                today(),
                request,
                expected_revision=body.expected_revision,
                dry_run=body.dry_run,
                force=body.force,
            )
        )

    @app.post("/plan/replan", response_model=MutationResult, tags=["changes"])
    def replan(body: MutationBody, _: Auth) -> MutationResult:
        """Rebuild the plan from today after an edit of profile.yaml."""
        return _refused(
            service.replan(
                today(),
                expected_revision=body.expected_revision,
                dry_run=body.dry_run,
                force=body.force,
            )
        )

    @app.get("/plan/amendments", response_model=list[AmendmentView], tags=["changes"])
    def amendments(_: Auth) -> list[AmendmentView]:
        """The amendment log."""
        return service.amendments()

    @app.post(
        "/plan/amendments/{amendment_id}/undo", response_model=MutationResult, tags=["changes"]
    )
    def undo(amendment_id: str, body: MutationBody, _: Auth) -> MutationResult:
        """Remove an amendment and replan from today."""
        return _refused(
            service.undo(
                today(),
                amendment_id,
                expected_revision=body.expected_revision,
                dry_run=body.dry_run,
                force=body.force,
            )
        )

    @app.post("/plan/push", response_model=PushReport, tags=["intervals.icu"])
    def push(body: PushBody, _: Auth) -> PushReport:
        """Send the workouts to intervals.icu (dry run by default: the payloads only)."""
        request = services.PushRequest(
            dry_run=body.dry_run,
            start=body.start,
            end=body.end,
            absolute_targets=body.absolute_targets,
        )
        try:
            return service.push(today(), request)
        except ValueError as exc:
            if "not configured" in str(exc):
                raise ApiError(503, "intervals_not_configured", str(exc)) from exc
            raise

    if web_dir is not None and (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def serve(
    data_dir: Path, host: str = LOCAL_HOST, port: int = 8000, insecure_local: bool = False
) -> None:
    """Run the API with uvicorn (token from PLANNER_TOKEN)."""
    import uvicorn

    token = os.environ.get("PLANNER_TOKEN") or None
    check_server_settings(token, host, insecure_local)
    app = create_app(data_dir, token=token, insecure_local=insecure_local and not token)
    uvicorn.run(app, host=host, port=port, log_level="info", server_header=False)
