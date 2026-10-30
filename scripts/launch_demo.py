#!/usr/bin/env python3
"""Prepare and launch the local, offline EquityTrace research demo (Streamlit).

    uv run --extra demo python scripts/launch_demo.py

Press "Load synthetic demo" on the Overview screen (or pass ``--prepare``).

The demo needs no SEC email, no market-data key and no internet access. It binds
to 127.0.0.1 only, disables Streamlit usage-statistics reporting, and keeps its
synthetic DuckDB under ``data/demo/`` (gitignored) unless ``--directory`` or
``EQUITYTRACE_DEMO_DIR`` says otherwise. It never touches the database named by
``EQUITYTRACE_DATABASE_PATH``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "equitytrace" / "ui" / "app.py"


def build_command(*, port: int, headless: bool) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true" if headless else "false",
        "--browser.gatherUsageStats",
        "false",
        "--client.toolbarMode",
        "minimal",
    ]


def build_environment(directory: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["EQUITYTRACE_DEMO_DIR"] = str(directory)
    # Prefer this checkout over any previously installed copy of the package.
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--directory", type=Path, default=None, help="Demo database directory.")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser tab.")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Create/verify the synthetic database before starting (the UI can also do it).",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create/verify the synthetic database and exit without starting the UI.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild an existing demo database (deletes saved demo runs).",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT / "src"))
    from equitytrace.demo.prepare import DemoPreparationError
    from equitytrace.demo.service import DemoService, default_demo_directory

    directory = (args.directory or default_demo_directory()).resolve()
    if args.prepare or args.prepare_only or args.rebuild:
        try:
            outcome = DemoService(directory).prepare(overwrite=args.rebuild)
        except DemoPreparationError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Synthetic demo {outcome.action}: {outcome.paths.database}")
        print(f"Content digest: {outcome.digest}")
        if args.prepare_only:
            return 0

    if importlib.util.find_spec("streamlit") is None:
        print(
            "Error: Streamlit is not installed. Run:\n"
            "  uv run --extra demo python scripts/launch_demo.py",
            file=sys.stderr,
        )
        return 1
    command = build_command(port=args.port, headless=args.no_browser)
    print(f"Starting EquityTrace on http://127.0.0.1:{args.port} (Ctrl+C to stop)")
    return subprocess.call(command, env=build_environment(directory))


if __name__ == "__main__":
    raise SystemExit(main())
