"""Adapter: entry point of the `endurance-planner` script (the CLI needs the `cli` extra)."""

import sys


def main() -> None:
    try:
        from endurance_planner.cli import app
    except ModuleNotFoundError as exc:
        if exc.name in ("typer", "click"):
            sys.stderr.write(
                'the command line needs the cli extra: pip install "endurance-planner[cli]"\n'
            )
            raise SystemExit(1) from exc
        raise
    app()
