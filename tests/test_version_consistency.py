"""Keep the declared package version aligned across pyproject and runtime."""

from __future__ import annotations

import tomllib
from pathlib import Path

import equitytrace


def test_pyproject_and_runtime_versions_match() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = data["project"]["version"]
    assert equitytrace.__version__ == declared
