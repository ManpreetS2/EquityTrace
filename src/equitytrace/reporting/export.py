"""Deterministic JSON exports for persisted research runs."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from equitytrace.portfolio.models import BacktestResult
from equitytrace.strategy.models import fingerprint_request


class ReportingError(ValueError):
    """User-facing reporting / export failure."""


def _json_dumps(payload: dict[str, Any]) -> str:
    """Stable UTF-8 JSON with sorted keys and a trailing newline."""
    return (
        json.dumps(
            payload,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def render_request_export(result: BacktestResult) -> str:
    """Serialize the stored BacktestRequest plus export metadata."""
    fingerprint = fingerprint_request(result.request)
    payload: dict[str, Any] = {
        "export_metadata": {
            "economic_fingerprint": fingerprint,
            "run_id": result.run_id,
        },
        "request": result.request.model_dump(mode="json"),
    }
    return _json_dumps(payload)


def render_result_export(result: BacktestResult) -> str:
    """Serialize the full persisted BacktestResult plus export metadata."""
    fingerprint = fingerprint_request(result.request)
    payload: dict[str, Any] = {
        "export_metadata": {
            "economic_fingerprint": fingerprint,
            "run_id": result.run_id,
        },
        "result": result.model_dump(mode="json"),
    }
    return _json_dumps(payload)


def _atomic_write_text(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise ReportingError(f"Output already exists at {path}. Pass --force to overwrite.")
    parent = path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReportingError(f"Cannot create output directory {parent}: {exc}") from exc
    if not parent.is_dir():
        raise ReportingError(f"Output parent is not a directory: {parent}")
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise ReportingError(f"Failed to write {path}: {exc}") from exc


def export_run_json(
    result: BacktestResult,
    output_dir: Path,
    *,
    force: bool = False,
) -> tuple[Path, Path]:
    """Write ``request.json`` and ``result.json`` under ``output_dir``."""
    if output_dir.exists() and not output_dir.is_dir():
        raise ReportingError(f"Export output path is not a directory: {output_dir}")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportingError(f"Cannot create export directory {output_dir}: {exc}") from exc

    request_path = output_dir / "request.json"
    result_path = output_dir / "result.json"
    for path in (request_path, result_path):
        if path.exists() and not force:
            raise ReportingError(f"Output already exists at {path}. Pass --force to overwrite.")

    _atomic_write_text(request_path, render_request_export(result), force=True)
    _atomic_write_text(result_path, render_result_export(result), force=True)
    return request_path, result_path


def write_html_report(path: Path, html: str, *, force: bool = False) -> Path:
    """Atomically write a standalone HTML report."""
    if path.exists() and path.is_dir():
        raise ReportingError(f"Report output path is a directory: {path}")
    _atomic_write_text(path, html, force=force)
    return path
