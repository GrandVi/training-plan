"""The web app (PWA): served by the API with the right content types, self-contained,
installable, readable (contrast AA in light and dark)."""

import json
import re
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from builders import EXAMPLE, TODAY
from endurance_planner import api

WEB = Path(api.__file__).parent / "web"
TOKEN = "web-token-0123456789abcdef"
CONTENT_TYPES = {
    "/": "text/html",
    "/index.html": "text/html",
    "/app.css": "text/css",
    "/app.js": "text/javascript",
    "/api.js": "text/javascript",
    "/dom.js": "text/javascript",
    "/sw.js": "text/javascript",
    "/manifest.json": "application/json",
    "/icons/icon-192.png": "image/png",
    "/icons/icon-512.png": "image/png",
    "/icons/icon-maskable-512.png": "image/png",
    "/icons/icon.svg": "image/svg+xml",
}


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    data = tmp_path_factory.mktemp("data")
    (data / "profile.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    app = api.create_app(data, token=TOKEN, today=lambda: TODAY, env={})
    return TestClient(app)


@pytest.mark.parametrize("path", CONTENT_TYPES)
def test_files_are_served_with_their_content_type(client: TestClient, path: str) -> None:
    response = client.get(path)  # no token: the app shell holds no data
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == CONTENT_TYPES[path]
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_data_still_needs_the_token(client: TestClient) -> None:
    assert client.get("/plan").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/no-such-file.js").status_code == 404


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_manifest_makes_an_installable_app(client: TestClient) -> None:
    manifest = client.get("/manifest.json").json()
    assert manifest["display"] == "standalone"
    assert (manifest["start_url"], manifest["scope"]) == ("/", "/")
    assert manifest["name"] and manifest["short_name"] and manifest["theme_color"]
    sizes = set()
    for item in manifest["icons"]:
        response = client.get(item["src"])
        assert response.status_code == 200 and response.headers["content-type"] == item["type"]
        if item["type"] == "image/png":
            width, height = _png_size(response.content)
            assert item["sizes"] == f"{width}x{height}"
            sizes.add(item["sizes"])
    assert {"192x192", "512x512"} <= sizes
    assert any(item.get("purpose") == "maskable" for item in manifest["icons"])


def test_service_worker_caches_the_app_shell(client: TestClient) -> None:
    worker = (WEB / "sw.js").read_text(encoding="utf-8")
    match = re.search(r"const SHELL = (\[.*?\]);", worker, re.S)
    assert match is not None
    shell = json.loads(match.group(1).replace(",\n]", "\n]"))
    assert {
        "/",
        "/index.html",
        "/app.js",
        "/api.js",
        "/dom.js",
        "/app.css",
        "/manifest.json",
    } <= set(shell)
    for path in shell:
        assert client.get(path).status_code == 200, path
    assert 'request.method !== "GET"' in worker  # changes always go to the network
    assert "(plan|profile)" in worker and "X-Served-From" in worker  # last plan, offline


def test_no_external_resources_or_inline_code() -> None:
    allowed = {"http://www.w3.org/2000/svg"}  # SVG namespace, not a download
    for path in WEB.rglob("*"):
        if path.suffix not in (".html", ".css", ".js", ".json", ".svg"):
            continue
        text = path.read_text(encoding="utf-8")
        urls = set(re.findall(r"https?://[^\s\"')]+", text)) - allowed
        assert urls == set(), (path.name, urls)
        assert "//cdn" not in text and "@import" not in text
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script(?![^>]*\bsrc=)", page) == []  # CSP: no inline script
    assert " style=" not in page and "<style" not in page  # CSP: no inline style
    assert '<html lang="en">' in page and 'name="viewport"' in page
    assert "innerHTML" not in (WEB / "app.js").read_text(encoding="utf-8")


def test_token_storage_survives_a_blocked_local_storage() -> None:
    source = (WEB / "api.js").read_text(encoding="utf-8")
    uses = [m.start() for m in re.finditer(r"localStorage\.", source)]
    assert len(uses) == 3
    for position in uses:  # every access is inside a try block
        before = source[:position]
        assert before.rfind("try {") > before.rfind("}\n}")


def _variables(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def _luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# (text, background) pairs used by the stylesheet.
PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("muted", "bg"),
    ("muted", "surface"),
    ("accent", "bg"),
    ("accent", "surface"),
    ("accent-text", "accent"),
    ("hard", "surface"),
    ("ok", "surface"),
    ("ok", "bg"),
    ("warn", "warn-bg"),
    ("error", "error-bg"),
    ("bg", "error"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_colours_meet_wcag_aa(theme: str) -> None:
    css = (WEB / "app.css").read_text(encoding="utf-8")
    light = _variables(css[: css.index("@media (prefers-color-scheme: dark)")])
    dark_block = css[css.index("@media (prefers-color-scheme: dark)") :]
    colours = {
        **light,
        **(_variables(dark_block[: dark_block.index("}\n}")]) if theme == "dark" else {}),
    }
    for text, background in PAIRS:
        ratio = contrast(colours[text], colours[background])
        assert ratio >= 4.5, f"{theme}: {text} on {background} = {ratio:.2f}"
    assert contrast(colours["focus"], colours["bg"]) >= 3  # focus ring (non-text contrast)
