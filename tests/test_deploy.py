"""Deployment files keep their safety properties (local bind, token, hardening, no secret)."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEPLOY = ROOT / "deploy"


def _unit() -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in (DEPLOY / "endurance-planner.service").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), []).append(value.strip())
    return values


def test_systemd_unit() -> None:
    unit = _unit()
    assert unit["User"] == ["planner"] and unit["Group"] == ["planner"]
    assert unit["EnvironmentFile"] == ["/etc/endurance-planner.env"]
    assert unit["Restart"] == ["on-failure"]
    [command] = unit["ExecStart"]
    assert " serve " in command and "--host 127.0.0.1" in command
    assert "--data-dir /var/lib/endurance-planner" in command
    for key, value in {
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "RestrictSUIDSGID": "yes",
        "CapabilityBoundingSet": "",
    }.items():
        assert unit[key] == [value], key
    assert unit["ReadWritePaths"] == ["/var/lib/endurance-planner"]


def test_environment_example_holds_no_secret() -> None:
    text = (DEPLOY / "endurance-planner.env.example").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.M))
    assert set(values) == {
        "PLANNER_TOKEN",
        "PLANNER_DATA_DIR",
        "INTERVALS_API_KEY",
        "INTERVALS_ATHLETE_ID",
    }
    assert values["PLANNER_TOKEN"] == "" and values["INTERVALS_API_KEY"] == ""
    assert values["PLANNER_DATA_DIR"] == "/var/lib/endurance-planner"


def test_dockerfile() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.\d+-slim$", text, re.M)  # official multi-arch image
    assert "--platform linux/arm64,linux/amd64" in text
    assert re.search(r"^USER planner$", text, re.M)  # not root
    assert '["endurance-planner", "serve"]' in text
    assert "-p 127.0.0.1:8000:8000" in text  # published on the host only
    assert "PLANNER_TOKEN=" not in text and "INTERVALS_API_KEY=" not in text
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", "profile.yaml", ".git"} <= set(ignored)
