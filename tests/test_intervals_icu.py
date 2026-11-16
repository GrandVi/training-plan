"""intervals.icu export: text rendering (golden files), payloads, client behaviour (mocked HTTP)."""

import base64
import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from builders import TODAY, golden_session, make_profile, run_10k_profile, session, step
from endurance_planner.export.intervals_icu import (
    IntervalsClient,
    IntervalsError,
    clear_events,
    event_payload,
    external_id,
    load_env_file,
    one_level,
    plan_events,
    push_plan,
    remote_thresholds,
    threshold_diffs,
    update_yaml_scalars,
    workout_text,
)
from endurance_planner.models.enums import Slot
from endurance_planner.models.plan import Plan, Repeat, Step
from endurance_planner.planner import generate_plan
from endurance_planner.templates import TEMPLATES, main_set
from endurance_planner.thresholds import ResolvedThresholds, resolve_thresholds

GOLDEN = Path(__file__).parent / "golden"
CASES = {
    "bike_threshold": ("bike_threshold", 2, False, None),
    "bike_threshold_absolute": ("bike_threshold", 2, True, None),
    "bike_over_unders": ("bike_over_unders", 1, False, None),
    "bike_vo2_30_15": ("bike_vo2_30_15", 0, False, None),
    "run_race_pace": ("run_race_pace", 0, False, None),
    "run_race_pace_absolute": ("run_race_pace", 0, True, None),
    "run_800s": ("run_800s", 0, False, None),
    "run_hills": ("run_hills", 0, False, None),
    "run_tempo_hr_mode": ("run_tempo", 0, False, "hr"),
}


@pytest.fixture(scope="module")
def plan() -> Plan:
    return generate_plan(run_10k_profile(), TODAY)


# ---------------------------------------------------------------------- text
@pytest.mark.parametrize("name", CASES)
def test_workout_text_matches_golden_file(name: str) -> None:
    template_id, level, absolute, mode = CASES[name]
    text = workout_text(golden_session(template_id, level, mode), absolute)
    assert text == (GOLDEN / f"{name}.txt").read_text(encoding="utf-8")


def test_workout_text_syntax_rules() -> None:
    text = workout_text(golden_session("bike_threshold", 2))
    lines = text.splitlines()
    assert "- Warm-up 15m 50-70%" in lines
    index = lines.index("3x")
    assert lines[index - 1] == "" and lines[index + 3] == ""  # empty line around a repeat
    assert lines[index + 1 : index + 3] == ["- 12m 95-100%", "- Recovery 5m 45-55%"]
    assert lines[-1] == "- Cool-down 10m 45-55%"
    # Units: metres are "mtr" ("m" means minutes), pace is "% Pace", HR is "% LTHR".
    assert "- 800mtr 107-111% Pace" in workout_text(golden_session("run_800s", 0))
    assert "- 1km 4:04-4:08/km Pace" in workout_text(golden_session("run_race_pace", 0), True)
    assert "- 8m 97-101% LTHR" in workout_text(golden_session("run_tempo", 0, "hr"))
    assert "- 12m 247-260w" in workout_text(golden_session("bike_threshold", 2), True)


def test_free_text_cannot_be_parsed_as_structure() -> None:
    odd = session([step(600, 0.6, 0.7)]).model_copy(
        update={"rationale": "- Do 3x10 today", "notes": "Main set 6x"}
    )
    lines = workout_text(odd).splitlines()
    assert lines[0] == "Do 3 x10 today"
    assert lines[1] == "Notes: Main set 6 x"
    assert [line for line in lines if line.startswith("-")] == ["- 10m 60-70%"]


def test_rpe_steps_are_pushed_as_equivalent_targets() -> None:
    text = workout_text(golden_session("run_hills", 0))
    assert "- 45s 103-109% Pace" in text  # RPE 8-9 on the run scale
    assert "RPE 8 to 9 out of 10" in text and "uphill 4-6%" in text


def test_nested_repeats_are_flattened_to_one_level() -> None:
    over_unders = one_level(main_set(TEMPLATES["bike_over_unders"], 1))  # 3 x (3 x (2+2) + 5)
    assert len(over_unders) == 1
    block = over_unders[0]
    assert isinstance(block, Repeat) and block.count == 3  # outer kept, inner written out
    assert [s.duration_s for s in block.steps if isinstance(s, Step)] == [120] * 6 + [300]
    short = one_level(main_set(TEMPLATES["bike_vo2_30_15"], 0))  # 2 x (8 x (30/15) + 5 min)
    counts = [b.count if isinstance(b, Repeat) else b.duration_s for b in short]
    assert counts == [8, 300, 8, 300]  # outer written out, inner repeats kept
    for blocks in (over_unders, short):
        for item in blocks:
            if isinstance(item, Repeat):
                assert all(isinstance(child, Step) for child in item.steps)


# ---------------------------------------------------------------------- payloads
def test_event_payload(plan: Plan) -> None:
    first = plan.weeks[0].sessions[0]
    payload = event_payload(plan.id, first)
    assert payload == {
        "category": "WORKOUT",
        "start_date_local": "2026-11-17T00:00:00",
        "type": "Run",
        "name": first.name,
        "description": workout_text(first),
        "external_id": "endurance-planner:victor-20261116:2026-11-17:key_1",
    }
    assert external_id(plan.id, first) == payload["external_id"]


def test_plan_events_window_and_unique_ids(plan: Plan) -> None:
    events = plan_events(plan)
    ids = [event["external_id"] for event in events]
    assert len(events) == sum(len(week.sessions) for week in plan.weeks)
    assert len(set(ids)) == len(ids)
    window = plan_events(plan, dt.date(2026, 11, 23), dt.date(2026, 11, 29))
    assert len(window) == len(plan.weeks[1].sessions)
    assert plan_events(plan) == plan_events(plan)  # deterministic: re-push is an update


# ---------------------------------------------------------------------- client
class FakeApi:
    """In-memory intervals.icu: a calendar keyed by event id, upsert on external_id."""

    def __init__(self) -> None:
        self.events: dict[int, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.failures: list[int | Exception] = []  # served before real answers
        self.next_id = 100
        self.athlete: dict[str, Any] = {
            "id": "i123",
            "sportSettings": [
                {"types": ["Ride", "VirtualRide"], "ftp": 265, "lthr": 168, "max_hr": 188},
                {"types": ["Run", "TrailRun"], "threshold_pace": 4.0, "lthr": 172, "max_hr": 190},
            ],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, text="simulated failure")
        path = request.url.path
        if request.method == "GET" and path.endswith("/athlete/0"):
            return httpx.Response(200, json=self.athlete)
        if request.method == "POST" and path.endswith("/events/bulk"):
            assert request.url.params["upsert"] == "true"
            saved = []
            for event in json.loads(request.content):
                match = [
                    key
                    for key, value in self.events.items()
                    if value.get("external_id") == event["external_id"]
                ]
                key = match[0] if match else self.next_id
                self.next_id += not match
                self.events[key] = {**event, "id": key}
                saved.append(self.events[key])
            return httpx.Response(200, json=saved)
        if request.method == "GET" and path.endswith("/events"):
            oldest, newest = request.url.params["oldest"], request.url.params["newest"]
            found = [
                event
                for event in self.events.values()
                if oldest <= event["start_date_local"][:10] <= newest
            ]
            return httpx.Response(200, json=found)
        if request.method == "PUT" and path.endswith("/events/bulk-delete"):
            for item in json.loads(request.content):
                self.events.pop(item["id"], None)
            return httpx.Response(200, json={})
        return httpx.Response(404, text="unknown route")


def _client(api: FakeApi, **kwargs: Any) -> tuple[IntervalsClient, list[float]]:
    sleeps: list[float] = []
    client = IntervalsClient(
        "secret-key", transport=httpx.MockTransport(api), sleep=sleeps.append, **kwargs
    )
    return client, sleeps


def test_basic_auth_and_endpoints(plan: Plan) -> None:
    api = FakeApi()
    client, _ = _client(api)
    client.upsert_events(plan_events(plan)[:2])
    request = api.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://intervals.icu/api/v1/athlete/0/events/bulk?upsert=true"
    token = base64.b64encode(b"API_KEY:secret-key").decode()
    assert request.headers["authorization"] == f"Basic {token}"
    client.list_events(dt.date(2026, 11, 16), dt.date(2026, 11, 22))
    assert api.requests[1].url.params["oldest"] == "2026-11-16"
    client.delete_events([100])
    assert api.requests[2].method == "PUT"
    assert json.loads(api.requests[2].content) == [{"id": 100}]
    before = len(api.requests)
    client.upsert_events([])
    client.delete_events([])
    assert len(api.requests) == before  # nothing to send, no request


def test_push_is_idempotent(plan: Plan) -> None:
    api = FakeApi()
    client, _ = _client(api)
    first = push_plan(client, plan)
    count = sum(len(week.sessions) for week in plan.weeks)
    assert (first.upserted, first.pruned) == (count, 0)
    snapshot = dict(api.events)
    again = push_plan(client, plan)
    assert (again.upserted, again.pruned) == (count, 0)
    assert api.events == snapshot  # same ids, same content: updated, not duplicated


def test_push_window_and_prune(plan: Plan) -> None:
    api = FakeApi()
    client, _ = _client(api)
    api.events[1] = {"id": 1, "start_date_local": "2026-11-18T00:00:00", "name": "My own note"}
    api.events[2] = {
        "id": 2,
        "start_date_local": "2026-11-19T00:00:00",
        "name": "Other plan",
        "external_id": "endurance-planner:other-plan:2026-11-19:easy",
    }
    api.events[3] = {
        "id": 3,
        "start_date_local": "2026-11-20T00:00:00",
        "name": "Stale session of this plan",
        "external_id": f"endurance-planner:{plan.id}:2026-11-20:key_3",
    }
    result = push_plan(client, plan, start=dt.date(2026, 11, 16), end=dt.date(2026, 11, 22))
    assert result.upserted == len(plan.weeks[0].sessions)
    assert result.pruned == 1
    assert 3 not in api.events  # stale event of this plan removed
    assert 1 in api.events and 2 in api.events  # everything else untouched
    kept = push_plan(
        client, plan, start=dt.date(2026, 11, 16), end=dt.date(2026, 11, 22), prune=False
    )
    assert kept.pruned == 0


def test_clear_removes_only_tool_events(plan: Plan) -> None:
    api = FakeApi()
    client, _ = _client(api)
    push_plan(client, plan)
    api.events[1] = {"id": 1, "start_date_local": "2026-11-18T00:00:00", "name": "My own note"}
    api.events[2] = {
        "id": 2,
        "start_date_local": "2026-11-19T00:00:00",
        "external_id": "endurance-planner:other-plan:2026-11-19:easy",
    }
    start, end = dt.date(2026, 11, 16), dt.date(2027, 2, 7)
    listed = clear_events(client, start, end, plan.id, dry_run=True)
    assert len(listed) == sum(len(week.sessions) for week in plan.weeks)
    assert len(api.events) == len(listed) + 2  # dry run deletes nothing
    clear_events(client, start, end, plan.id)
    assert sorted(api.events) == [1, 2]
    clear_events(client, start, end)  # every plan of this tool
    assert sorted(api.events) == [1]


def test_retries_with_backoff_on_429_5xx_and_timeouts() -> None:
    api = FakeApi()
    api.failures = [429, 503, httpx.ReadTimeout("slow")]
    logs: list[str] = []
    client, sleeps = _client(api, log=logs.append)
    assert client.athlete()["id"] == "i123"
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(api.requests) == 4
    assert any("retry 3/4" in line and "ReadTimeout" in line for line in logs)
    assert any(line.startswith("> GET /athlete/0") for line in logs)
    assert any(line.startswith("< 200") for line in logs)
    assert not any("secret-key" in line for line in logs)


def test_gives_up_after_max_retries() -> None:
    api = FakeApi()
    api.failures = [500] * 10
    client, sleeps = _client(api, max_retries=2)
    with pytest.raises(IntervalsError, match="did not answer after 3 attempts \\(HTTP 500\\)"):
        client.athlete()
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "authentication failed: check INTERVALS_API_KEY"),
        (403, "access denied"),
        (422, "rejected the data - simulated failure"),
        (404, "not found: check the athlete id"),
        (418, "unexpected response"),
    ],
)
def test_client_errors_are_explained_and_not_retried(status: int, message: str) -> None:
    api = FakeApi()
    api.failures = [status]
    client, sleeps = _client(api)
    with pytest.raises(IntervalsError, match=message):
        client.athlete()
    assert sleeps == [] and len(api.requests) == 1


# ---------------------------------------------------------------------- thresholds
def test_remote_thresholds_and_diffs() -> None:
    remote = remote_thresholds(FakeApi().athlete)
    assert remote.ftp_w == 265
    assert remote.threshold_pace_s == pytest.approx(250)  # 4.0 m/s
    assert (remote.bike_lthr_bpm, remote.run_max_hr_bpm) == (168, 190)
    local = resolve_thresholds(make_profile(), TODAY)[0]
    diffs = threshold_diffs(local, remote)
    assert [diff.path[-1] for diff in diffs] == ["ftp_w", "threshold_pace"]
    assert diffs[0].label == "bike FTP (W): profile 260, intervals.icu 265"
    assert diffs[1].label == "run threshold pace: profile 4:15, intervals.icu 4:10"
    assert (diffs[0].remote, diffs[1].remote) == ("265", '"4:10"')
    assert threshold_diffs(local, local) == []
    assert threshold_diffs(local, ResolvedThresholds()) == []  # nothing set remotely
    assert remote_thresholds({}) == ResolvedThresholds()


def test_update_yaml_scalars_keeps_comments() -> None:
    text = (
        "thresholds:\n"
        "  bike:\n"
        "    ftp_w: 260                  # required\n"
        "    lthr_bpm: 168\n"
        "  run:\n"
        '    threshold_pace: "4:15"      # min/km\n'
        "    lthr_bpm: 172\n"
        "goals:\n"
        "  - name: x\n"
    )
    updated, missing = update_yaml_scalars(
        text,
        {
            ("thresholds", "bike", "ftp_w"): "265",
            ("thresholds", "run", "threshold_pace"): '"4:10"',
            ("thresholds", "run", "max_hr_bpm"): "190",
        },
    )
    assert "    ftp_w: 265                  # required\n" in updated
    assert '    threshold_pace: "4:10"      # min/km\n' in updated
    assert "    lthr_bpm: 168\n" in updated and "    lthr_bpm: 172\n" in updated
    assert missing == ["thresholds.run.max_hr_bpm"]


def test_load_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\nINTERVALS_API_KEY="abc"\nINTERVALS_ATHLETE_ID=i42\n\nbroken\n')
    assert load_env_file(str(env)) == {"INTERVALS_API_KEY": "abc", "INTERVALS_ATHLETE_ID": "i42"}
    assert load_env_file(str(tmp_path / "missing")) == {}


def test_slots_make_ids_stable_across_swaps(plan: Plan) -> None:
    first = plan.weeks[0].sessions[0]
    assert first.slot is Slot.KEY_1
    renamed = first.model_copy(update={"name": "Something else"})
    assert external_id(plan.id, renamed) == external_id(plan.id, first)
