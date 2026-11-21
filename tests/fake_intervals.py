"""In-memory intervals.icu behind the core FakeTransport (no network)."""

from typing import Any
from urllib.parse import urlparse

from endurance_planner.export.intervals_icu import IntervalsClient
from endurance_planner.transport import FakeTransport, Request, Response, json_response

ATHLETE = {
    "id": "i123",
    "sportSettings": [
        {"types": ["Ride", "VirtualRide"], "ftp": 265, "lthr": 168, "max_hr": 188},
        {"types": ["Run", "TrailRun"], "threshold_pace": 4.0, "lthr": 172, "max_hr": 190},
    ],
}


class FakeIntervals:
    """Calendar keyed by event id, upsert on external_id; records the clients' API keys."""

    def __init__(self) -> None:
        self.events: dict[int, dict[str, Any]] = {}
        self.next_id = 100
        self.transport = FakeTransport(self.handle)
        self.closed = 0

    def client(self, api_key: str = "test-key") -> IntervalsClient:
        return IntervalsClient(api_key, transport=self.transport, sleep=lambda _: None)

    def handle(self, request: Request) -> Response:
        path = urlparse(request.url).path
        params = request.params or {}
        if request.method == "GET" and path.endswith("/athlete/0"):
            return json_response(ATHLETE)
        if request.method == "POST" and path.endswith("/events/bulk"):
            saved = []
            for event in request.json:
                match = [
                    k for k, v in self.events.items() if v["external_id"] == event["external_id"]
                ]
                key = match[0] if match else self.next_id
                self.next_id += not match
                self.events[key] = {**event, "id": key}
                saved.append(self.events[key])
            return json_response(saved)
        if request.method == "GET" and path.endswith("/events"):
            oldest, newest = params["oldest"], params["newest"]
            found = [
                e for e in self.events.values() if oldest <= e["start_date_local"][:10] <= newest
            ]
            return json_response(found)
        if request.method == "PUT" and path.endswith("/events/bulk-delete"):
            for item in request.json:
                self.events.pop(item["id"], None)
            return json_response({})
        return Response(404, text="unknown route")
