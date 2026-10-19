"""Load and serialize ResearchStrategy JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from equitytrace.strategy.models import ResearchStrategy


class StrategyIOError(ValueError):
    """Strategy file could not be loaded or parsed."""


def load_strategy(path: Path) -> ResearchStrategy:
    """Load and validate a ResearchStrategy from a UTF-8 JSON file."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StrategyIOError(f"Strategy file not found: {target}") from exc
    except UnicodeDecodeError as exc:
        raise StrategyIOError(f"Strategy file is not valid UTF-8: {target}") from exc
    except OSError as exc:
        raise StrategyIOError(f"Unable to read strategy file: {target}") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except StrategyIOError:
        raise
    except json.JSONDecodeError as exc:
        raise StrategyIOError(f"Invalid JSON in strategy file: {target}") from exc
    if not isinstance(payload, dict):
        raise StrategyIOError("Strategy JSON must be an object.")
    try:
        return ResearchStrategy.model_validate(payload)
    except ValidationError as exc:
        raise StrategyIOError(_format_validation(exc)) from exc


def strategy_to_canonical_json(strategy: ResearchStrategy) -> str:
    """Deterministic UTF-8 JSON for a validated ResearchStrategy."""
    payload = strategy.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def save_strategy(path: Path, strategy: ResearchStrategy) -> None:
    """Write a strategy as canonical UTF-8 JSON."""
    Path(path).write_text(strategy_to_canonical_json(strategy), encoding="utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object and reject repeated keys at this level."""
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise StrategyIOError(f"Duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _format_validation(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc", ()))
        msg = str(error.get("msg", "invalid"))
        if loc:
            parts.append(f"{loc}: {msg}")
        else:
            parts.append(msg)
    return "Invalid strategy: " + "; ".join(parts) if parts else "Invalid strategy."
