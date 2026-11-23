"""intervals.icu export: workout text, calendar events, API client.

Checked against the intervals.icu forum guides (November 2026):
- "Uploading planned workouts to Intervals.icu": POST /athlete/{id}/events/bulk?upsert=true
  upserts on external_id; PUT /athlete/{id}/events/bulk-delete deletes by id or external_id;
  athlete id 0 means "the athlete of the API key".
- "Workout Builder Syntax Quick Guide": steps are lines starting with "-", durations like
  5m30s, distances like 2km / 500mtr ("m" is minutes), "%" is % of FTP, "% LTHR",
  "% Pace" (of threshold pace), absolute "220w" and "5:00/km Pace", repeats as a standalone
  "5x" line with an empty line before and after the block, no nested repeats.
- API access thread: HTTP Basic auth with username API_KEY; sport settings (ftp, lthr,
  max_hr, threshold_pace in m/s) are in the `sportSettings` field of GET /athlete/{id}.

Not supported by the text syntax, and how it is handled here:
- RPE targets: pushed as the equivalent % of threshold, with a note in the description.
- Nested repeats (over-unders, 30/15 sets): flattened to one level.
- Absolute HR in bpm is not documented: HR targets stay in % LTHR even with absolute targets.
- Sets and reps (strength sessions): plain text in the description, type WeightTraining.
  UNVERIFIED: the planned duration and load are sent as `moving_time` (seconds) and
  `icu_training_load`, names taken from the activity fields; check them with a real account.
"""

import base64
import datetime as dt
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from endurance_planner.load import strength_tss
from endurance_planner.models.enums import ActivityType, Sport, StepKind, TargetKind
from endurance_planner.models.plan import Plan, Repeat, Session, Step, StrengthSession, flatten
from endurance_planner.strength import description as strength_description
from endurance_planner.thresholds import ResolvedThresholds
from endurance_planner.transport import Response, Transport, TransportError
from endurance_planner.units import format_clock
from endurance_planner.zones import rpe_to_if

BASE_URL = "https://intervals.icu/api/v1"
EXTERNAL_ID_PREFIX = "endurance-planner"
RETRY_STATUS = {429, 500, 502, 503, 504}
CUES = {
    StepKind.WARMUP: "Warm-up",
    StepKind.COOLDOWN: "Cool-down",
    StepKind.RECOVERY: "Recovery",
    StepKind.WORK: "",
}


# ---------------------------------------------------------------------- workout text
def _duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    text = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "")
    return text + (f"{secs}s" if secs or not text else "")


def _distance(metres: int) -> str:
    return f"{metres / 1000:g}km" if metres >= 1000 else f"{metres}mtr"


def _span(low: str, high: str, unit: str) -> str:
    return f"{low}{unit}" if low == high else f"{low}-{high}{unit}"


def _percent(low: float, high: float, unit: str) -> str:
    return _span(f"{low * 100:.0f}", f"{high * 100:.0f}", unit)


def target_text(step: Step, sport: Sport, absolute: bool = False) -> str:
    """Target of one step in intervals.icu syntax."""
    target, resolved = step.target, step.resolved
    native_unit = "%" if sport is Sport.BIKE else "% Pace"
    match target.kind:
        case TargetKind.POWER:
            if absolute and resolved is not None and resolved.power_w is not None:
                return _span(str(resolved.power_w[0]), str(resolved.power_w[1]), "w")
            return _percent(target.low, target.high, "%")
        case TargetKind.PACE:
            if absolute and resolved is not None and resolved.pace_s_per_km is not None:
                slow, fast = resolved.pace_s_per_km
                return _span(format_clock(fast), format_clock(slow), "/km Pace")
            return _percent(target.low, target.high, "% Pace")
        case TargetKind.HR:
            return _percent(target.low, target.high, "% LTHR")
        case _:  # RPE: no such target in intervals.icu
            return _percent(
                rpe_to_if(target.low, sport), rpe_to_if(target.high, sport), native_unit
            )


def _step_line(step: Step, sport: Sport, absolute: bool) -> str:
    size = _distance(step.distance_m) if step.distance_m else _duration(step.duration_s)
    parts = [CUES[step.kind], size, target_text(step, sport, absolute)]
    return "- " + " ".join(part for part in parts if part)


def one_level(blocks: list[Step | Repeat]) -> list[Step | Repeat]:
    """Flatten nested repeats to a single level (intervals.icu has no nested repeats).

    The outer repeat is kept when it has at least as many rounds as the inner ones
    (inner repeats are written out); otherwise the outer repeat is written out.
    """
    out: list[Step | Repeat] = []
    for block in blocks:
        if isinstance(block, Step) or all(isinstance(child, Step) for child in block.steps):
            out.append(block)
            continue
        children = one_level(block.steps)
        inner_counts = [child.count for child in children if isinstance(child, Repeat)]
        if block.count >= max(inner_counts):
            out.append(Repeat(count=block.count, steps=list(flatten(children))))
        else:
            out.extend(children * block.count)
    return out


def _safe(text: str) -> str:
    """Free text that cannot be read as a repeat header ("6x") or a step line."""
    return re.sub(r"(\d)x", r"\1 x", text).lstrip("- ")


def workout_text(session: Session, absolute: bool = False) -> str:
    """Description of a session in the intervals.icu workout text syntax."""
    lines = [_safe(session.rationale)]
    notes = list(dict.fromkeys(step.note for step in session.flat_steps() if step.note))
    if session.notes:
        notes.insert(0, session.notes)
    if notes:
        lines.append(_safe("Notes: " + "; ".join(notes)))
    efforts = [s.target for s in session.flat_steps() if s.target.kind is TargetKind.RPE]
    if efforts:
        low, high = min(t.low for t in efforts), max(t.high for t in efforts)
        scale = f"{low:g}/10" if low == high else f"{low:g} to {high:g} out of 10"
        lines.append(
            f"Effort-based steps: go by feel (RPE {scale}); the targets shown are an equivalent."
        )
    previous_was_repeat = True  # forces an empty line after the free text
    for block in one_level(session.steps):
        if isinstance(block, Repeat):
            lines += ["", f"{block.count}x"]
            lines += [_step_line(step, session.sport, absolute) for step in flatten(block.steps)]
            previous_was_repeat = True
        else:
            if previous_was_repeat:
                lines.append("")
            lines.append(_step_line(block, session.sport, absolute))
            previous_was_repeat = False
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- events
def plan_prefix(plan_id: str | None = None) -> str:
    """external_id prefix of the events of one plan, or of every plan pushed by this tool."""
    return f"{EXTERNAL_ID_PREFIX}:{plan_id}:" if plan_id else f"{EXTERNAL_ID_PREFIX}:"


STRENGTH_SLOT = "strength"


def external_id(plan_id: str, session: Session | StrengthSession) -> str:
    """Stable id (plan + date + slot): pushing again updates the event instead of duplicating.
    Strength sessions use the slot `strength`."""
    slot = STRENGTH_SLOT if isinstance(session, StrengthSession) else session.slot.value
    return f"{plan_prefix(plan_id)}{session.date.isoformat()}:{slot}"


def event_payload(plan_id: str, session: Session, absolute: bool = False) -> dict[str, Any]:
    return {
        "category": "WORKOUT",
        "start_date_local": f"{session.date.isoformat()}T00:00:00",
        "type": session.activity_type.value,
        "name": session.name,
        "description": workout_text(session, absolute),
        "external_id": external_id(plan_id, session),
    }


def strength_payload(plan_id: str, session: StrengthSession) -> dict[str, Any]:
    """A strength session as a WeightTraining workout: exercises as plain text, the flat
    load estimate as the event load (UNVERIFIED field names, see the module docstring)."""
    return {
        "category": "WORKOUT",
        "start_date_local": f"{session.date.isoformat()}T00:00:00",
        "type": ActivityType.WEIGHT_TRAINING.value,
        "name": session.name,
        "description": strength_description(session),
        "moving_time": session.minutes * 60,
        "icu_training_load": round(strength_tss(session.minutes)),
        "external_id": external_id(plan_id, session),
    }


def plan_events(
    plan: Plan,
    start: dt.date | None = None,
    end: dt.date | None = None,
    absolute: bool = False,
) -> list[dict[str, Any]]:
    """Event payloads of the sessions (then the strength sessions) between start and end
    (inclusive). Completed activities (plan adapt) are already in intervals.icu: not sent."""

    def inside(date: dt.date) -> bool:
        return (start is None or date >= start) and (end is None or date <= end)

    events = [
        event_payload(plan.id, session, absolute)
        for week in plan.weeks
        for session in week.sessions
        if inside(session.date) and not session.actual
    ]
    return events + [
        strength_payload(plan.id, session)
        for week in plan.weeks
        for session in week.strength
        if inside(session.date) and not session.actual
    ]


# ---------------------------------------------------------------------- client
class IntervalsError(Exception):
    """An intervals.icu request failed; the message says what to check."""


Logger = Callable[[str], None]


class IntervalsClient:
    """Minimal intervals.icu API client (API key, HTTP Basic with username API_KEY).

    HTTP goes through `transport` (see transport.Transport). For compatibility, an httpx
    transport (e.g. httpx.MockTransport) or no transport at all is wrapped in the httpx
    adapter, which needs the `http` extra.
    """

    def __init__(
        self,
        api_key: str,
        athlete_id: str = "0",
        *,
        base_url: str = BASE_URL,
        transport: Transport | object | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 4,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        log: Logger | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._log = log
        token = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._transport = transport if isinstance(transport, Transport) else _httpx(transport)

    def close(self) -> None:
        self._transport.close()

    def _say(self, message: str) -> None:
        if self._log is not None:
            self._log(message)

    def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None, json: Any = None
    ) -> Response:
        """Send a request; retry with exponential backoff on timeouts, 429 and 5xx."""
        url = f"/athlete/{self.athlete_id}{path}"
        problem = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.backoff_s * 2 ** (attempt - 1)
                self._say(f"retry {attempt}/{self.max_retries} in {delay:g} s ({problem})")
                self._sleep(delay)
            self._say(f"> {method} {url} params={params} body={json}")
            try:
                response = self._transport.request(
                    method,
                    self._base_url + url,
                    params=params,
                    json=json,
                    headers=self._headers,
                    timeout=self._timeout_s,
                )
            except TransportError as exc:
                problem = str(exc)
                continue
            self._say(f"< {response.status} {response.text[:2000]}")
            if response.status in RETRY_STATUS:
                problem = f"HTTP {response.status}"
                continue
            if response.is_success:
                return response
            raise IntervalsError(_explain(response))
        raise IntervalsError(
            f"intervals.icu did not answer after {self.max_retries + 1} attempts ({problem})"
        )

    def athlete(self) -> dict[str, Any]:
        data = self._request("GET", "").json()
        assert isinstance(data, dict)
        return data

    def upsert_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create or update events, matched on external_id."""
        if not events:
            return []
        response = self._request("POST", "/events/bulk", params={"upsert": "true"}, json=events)
        data = response.json()
        assert isinstance(data, list)
        return data

    def list_events(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        data = self._request("GET", "/events", params=params).json()
        assert isinstance(data, list)
        return data

    def list_activities(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        """Completed activities between oldest and newest (inclusive), as returned."""
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        data = self._request("GET", "/activities", params=params).json()
        if not isinstance(data, list):
            raise IntervalsError("intervals.icu returned no activity list")
        return [item for item in data if isinstance(item, dict)]

    def list_wellness(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        """Wellness records between oldest and newest (inclusive), as returned."""
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        data = self._request("GET", "/wellness", params=params).json()
        if not isinstance(data, list):
            raise IntervalsError("intervals.icu returned no wellness list")
        return [item for item in data if isinstance(item, dict)]

    def delete_events(self, event_ids: list[int]) -> None:
        if event_ids:
            body = [{"id": event_id} for event_id in event_ids]
            self._request("PUT", "/events/bulk-delete", json=body)


def _httpx(transport: object | None) -> Transport:
    """The httpx adapter around an httpx transport (or real HTTP when None)."""
    from endurance_planner.http_transport import HttpxTransport  # adapter, extra `http`

    return HttpxTransport(transport)  # type: ignore[arg-type]


def _explain(response: Response) -> str:
    status = response.status
    detail = response.text.strip()[:500]
    hints = {
        401: "authentication failed: check INTERVALS_API_KEY (Settings > Developer Settings)",
        403: "access denied: check the athlete id and that the API key may write the calendar",
        404: "not found: check the athlete id",
        422: "intervals.icu rejected the data",
    }
    hint = hints.get(status, "unexpected response")
    return f"intervals.icu HTTP {status}: {hint}" + (f" - {detail}" if detail else "")


# ---------------------------------------------------------------------- operations
@dataclass(frozen=True)
class PushResult:
    upserted: int
    pruned: int
    unchanged: int = 0  # events already identical in the calendar (only_changed)


def tool_events(
    client: IntervalsClient, oldest: dt.date, newest: dt.date, prefix: str
) -> list[dict[str, Any]]:
    """Calendar events in the window that were created by this tool (external_id prefix)."""
    return [
        event
        for event in client.list_events(oldest, newest)
        if str(event.get("external_id") or "").startswith(prefix)
    ]


def push_plan(
    client: IntervalsClient,
    plan: Plan,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    absolute: bool = False,
    prune: bool = True,
    only_changed: bool = False,
) -> PushResult:
    """Upsert the plan's sessions in the window. With `prune`, events of this plan that are
    in the window but no longer in the plan (e.g. after a regeneration) are deleted.

    With `only_changed`, the window is read first and events whose fields are already the
    ones this tool would send are left alone: after an amendment or a replan only the
    sessions that changed are written (their external_id did not change).
    """
    events = plan_events(plan, start, end, absolute)
    oldest = start or plan.weeks[0].start
    newest = end or plan.weeks[-1].start + dt.timedelta(days=6)
    existing: list[dict[str, Any]] | None = None
    to_send = events
    if only_changed:
        existing = tool_events(client, oldest, newest, plan_prefix(plan.id))
        remote = {event["external_id"]: event for event in existing}
        to_send = [event for event in events if not _same(remote.get(event["external_id"]), event)]
    client.upsert_events(to_send)
    stale: list[int] = []
    if prune:
        if existing is None:
            existing = tool_events(client, oldest, newest, plan_prefix(plan.id))
        current = {event["external_id"] for event in events}
        stale = [int(event["id"]) for event in existing if event["external_id"] not in current]
        client.delete_events(stale)
    return PushResult(
        upserted=len(to_send), pruned=len(stale), unchanged=len(events) - len(to_send)
    )


def _same(remote: dict[str, Any] | None, event: dict[str, Any]) -> bool:
    """True if the calendar event already has every field this tool sends."""
    return remote is not None and all(remote.get(key) == value for key, value in event.items())


def clear_events(
    client: IntervalsClient,
    oldest: dt.date,
    newest: dt.date,
    plan_id: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Delete the events created by this tool in the window (one plan, or all plans).
    Returns the events concerned; nothing else on the calendar is touched."""
    events = tool_events(client, oldest, newest, plan_prefix(plan_id))
    if not dry_run:
        client.delete_events([int(event["id"]) for event in events])
    return events


# ---------------------------------------------------------------------- thresholds
def remote_thresholds(athlete: dict[str, Any]) -> ResolvedThresholds:
    """Thresholds configured in intervals.icu (sportSettings of the athlete)."""

    def settings(activity_type: str) -> dict[str, Any]:
        for entry in athlete.get("sportSettings") or []:
            if activity_type in (entry.get("types") or []):
                assert isinstance(entry, dict)
                return entry
        return {}

    ride, run = settings("Ride"), settings("Run")
    speed = run.get("threshold_pace")  # metres per second
    return ResolvedThresholds(
        ftp_w=ride.get("ftp") or None,
        bike_lthr_bpm=ride.get("lthr") or None,
        bike_max_hr_bpm=ride.get("max_hr") or None,
        threshold_pace_s=1000 / speed if speed else None,
        run_lthr_bpm=run.get("lthr") or None,
        run_max_hr_bpm=run.get("max_hr") or None,
    )


@dataclass(frozen=True)
class ThresholdDiff:
    path: tuple[str, ...]  # location in profile.yaml
    label: str
    local: str | None  # value used by the plan (None = not set)
    remote: str  # value in intervals.icu, formatted as in profile.yaml


def threshold_diffs(local: ResolvedThresholds, remote: ResolvedThresholds) -> list[ThresholdDiff]:
    """Thresholds set in intervals.icu that differ from the profile's."""
    fields: list[tuple[str, tuple[str, ...], str, float]] = [
        ("ftp_w", ("thresholds", "bike", "ftp_w"), "bike FTP (W)", 0.5),
        ("bike_lthr_bpm", ("thresholds", "bike", "lthr_bpm"), "bike LTHR (bpm)", 0.5),
        ("bike_max_hr_bpm", ("thresholds", "bike", "max_hr_bpm"), "bike max HR (bpm)", 0.5),
        ("threshold_pace_s", ("thresholds", "run", "threshold_pace"), "run threshold pace", 1.5),
        ("run_lthr_bpm", ("thresholds", "run", "lthr_bpm"), "run LTHR (bpm)", 0.5),
        ("run_max_hr_bpm", ("thresholds", "run", "max_hr_bpm"), "run max HR (bpm)", 0.5),
    ]
    diffs: list[ThresholdDiff] = []
    for name, path, label, tolerance in fields:
        mine, theirs = getattr(local, name), getattr(remote, name)
        if theirs is None or (mine is not None and abs(mine - theirs) <= tolerance):
            continue
        pace = name == "threshold_pace_s"
        shown = format_clock(theirs) if pace else f"{theirs:.0f}"
        local_text = None
        if mine is not None:
            local_text = format_clock(mine) if pace else f"{mine:.0f}"
        diffs.append(
            ThresholdDiff(
                path=path,
                label=f"{label}: profile {local_text or 'not set'}, intervals.icu {shown}",
                local=local_text,
                remote=f'"{shown}"' if pace else shown,
            )
        )
    return diffs


def update_yaml_scalars(text: str, updates: dict[tuple[str, ...], str]) -> tuple[str, list[str]]:
    """Replace scalar values in a YAML text by key path, keeping comments and layout.

    Returns the new text and the dotted paths that were not found (to add by hand).
    Only block-style mappings are handled, which is what profile.yaml uses for thresholds.
    """
    pending = dict(updates)
    stack: list[tuple[int, str]] = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)([A-Za-z_][\w]*):(\s*)([^#\n]*?)(\s*#.*)?(\r?\n)?$", line)
        if not match or line.lstrip().startswith("#"):
            continue
        indent, key = len(match.group(1)), match.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        path = tuple(name for _, name in stack)
        if path in pending and match.group(4).strip():
            comment = match.group(5) or ""
            lines[index] = (
                f"{match.group(1)}{key}: {pending.pop(path)}{comment}{match.group(6) or ''}"
            )
    return "".join(lines), [".".join(path) for path in pending]


def __getattr__(name: str) -> Any:
    """load_env_file moved to the settings adapter (file access is not part of the core)."""
    if name == "load_env_file":
        from endurance_planner import settings

        return settings.load_env_file
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
