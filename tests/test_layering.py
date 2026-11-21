"""Architecture: a pure core and thin adapters.

The core (planning library and services) does no file, environment or clock access and
does not depend on the CLI, HTTP or web frameworks. Only the adapters listed here may.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "src" / "endurance_planner"
# Modules that touch the outside world: files, environment, HTTP, terminal, web server.
ADAPTERS = {
    "endurance_planner.cli",
    "endurance_planner.config",
    "endurance_planner.export.json_io",
    "endurance_planner.files",
    "endurance_planner.http_transport",
    "endurance_planner.launcher",
    "endurance_planner.settings",
    "endurance_planner.storage",
}
FRAMEWORKS = {"typer", "click", "httpx", "fastapi", "starlette", "uvicorn"}
SYSTEM = {"os", "pathlib", "shutil", "tempfile", "fcntl", "glob", "subprocess", "socket"}
FILE_METHODS = {"read_text", "write_text", "read_bytes", "write_bytes"}
CLOCK = {"today", "now", "utcnow"}
# Documented compatibility imports, done lazily inside a function (see README "Assumptions"):
# an httpx transport (or none) given to IntervalsClient is wrapped in the httpx adapter, and
# load_env_file is still importable from its old module.
LAZY_ADAPTER_IMPORTS = {
    ("endurance_planner.export.intervals_icu", "endurance_planner.http_transport"),
    ("endurance_planner.export.intervals_icu", "endurance_planner.settings"),
}


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


MODULES = {_module_name(path): path for path in sorted(PACKAGE.rglob("*.py"))}
CORE = sorted(set(MODULES) - ADAPTERS)


def _imports(tree: ast.AST, module: str) -> list[tuple[str, bool]]:
    """(imported module, at module level) for every import of the module."""
    found: list[tuple[str, bool]] = []
    top = {id(node) for node in getattr(tree, "body", [])}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, id(node) in top) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(module.split(".")[: -node.level] + ([base] if base else []))
            found.append((base, id(node) in top))
            found += [(f"{base}.{alias.name}", id(node) in top) for alias in node.names]
    return found


def test_every_listed_adapter_exists() -> None:
    assert set(MODULES) >= ADAPTERS
    assert "endurance_planner.services" in CORE


@pytest.mark.parametrize("module", CORE)
def test_core_does_not_import_frameworks_or_adapters(module: str) -> None:
    tree = ast.parse(MODULES[module].read_text(encoding="utf-8"))
    for name, _ in _imports(tree, module):
        assert name.split(".")[0] not in FRAMEWORKS, f"{module} imports {name}"
        assert name.split(".")[0] not in SYSTEM, f"{module} imports {name}"
        if name in ADAPTERS:
            assert (module, name) in LAZY_ADAPTER_IMPORTS, f"{module} imports adapter {name}"


@pytest.mark.parametrize("module", CORE)
def test_core_does_no_file_or_clock_access(module: str) -> None:
    tree = ast.parse(MODULES[module].read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            assert function.id not in ("open", "input"), f"{module} calls {function.id}()"
        elif isinstance(function, ast.Attribute):
            assert function.attr not in FILE_METHODS, f"{module} calls .{function.attr}()"
            target = ast.unparse(function.value)
            if function.attr in CLOCK:
                assert target.split(".")[-1] not in ("date", "datetime"), (
                    f"{module} reads the clock: {target}.{function.attr}(); pass `today`"
                )
            assert not (function.attr == "getenv" or target.endswith("environ")), module


def test_core_imports_without_the_adapter_packages() -> None:
    """`pip install endurance-planner` (no extras) is enough for the core."""
    blocked = sorted(FRAMEWORKS)
    code = (
        "import importlib, importlib.abc, sys\n"
        f"BLOCKED = {blocked!r}\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        f"for module in {CORE!r}:\n"
        "    importlib.import_module(module)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
