"""Narrow skfolio adapter: aligned ReturnMatrix → minimum-variance target weights."""

from __future__ import annotations

import math
from typing import Any

from skfolio import RiskMeasure
from skfolio.optimization import MeanRisk, ObjectiveFunction

from equitytrace.portfolio.models import REQUIRED_RETURNS, WEIGHT_TOLERANCE
from equitytrace.portfolio.returns import ReturnMatrix

OPTIMIZER_NUMERICAL_TOLERANCE = 1e-8


class OptimizerUnavailable(RuntimeError):
    """Minimum-variance target weights cannot be formed."""

    def __init__(self, reason: str, *, diagnostic: str | None = None) -> None:
        self.reason = reason
        self.diagnostic = diagnostic
        super().__init__(reason)


def mean_risk_minimum_variance_model() -> MeanRisk:
    """Single v0.3c optimizer configuration (long-only, fully invested min variance)."""
    return MeanRisk(
        objective_function=ObjectiveFunction.MINIMIZE_RISK,
        risk_measure=RiskMeasure.VARIANCE,
        min_weights=0.0,
        max_weights=1.0,
        budget=1.0,
        transaction_costs=0.0,
        management_fees=0.0,
        solver="CLARABEL",
        fallback=None,
        raise_on_failure=False,
    )


def _canonical_symbols(matrix: ReturnMatrix) -> tuple[str, ...]:
    """Sort and validate symbols; reject blanks and duplicates."""
    if not matrix.returns:
        raise OptimizerUnavailable("optimizer_input_invalid")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in matrix.symbols:
        symbol = raw.strip().upper()
        if not symbol:
            raise OptimizerUnavailable("optimizer_input_invalid")
        if symbol in seen:
            raise OptimizerUnavailable("optimizer_input_invalid")
        seen.add(symbol)
        cleaned.append(symbol)
    if not cleaned:
        raise OptimizerUnavailable("optimizer_input_invalid")
    return_keys: set[str] = set()
    for raw_key in matrix.returns:
        key = raw_key.strip().upper()
        if not key or key in return_keys:
            raise OptimizerUnavailable("optimizer_input_invalid")
        return_keys.add(key)
    if return_keys != set(cleaned):
        raise OptimizerUnavailable("optimizer_input_invalid")
    return tuple(sorted(cleaned))


def _validated_series(
    matrix: ReturnMatrix,
    symbols: tuple[str, ...],
) -> dict[str, tuple[float, ...]]:
    by_symbol = {key.strip().upper(): series for key, series in matrix.returns.items()}
    length: int | None = None
    out: dict[str, tuple[float, ...]] = {}
    for symbol in symbols:
        series = by_symbol.get(symbol)
        if series is None:
            raise OptimizerUnavailable("optimizer_input_invalid")
        if length is None:
            length = len(series)
        elif len(series) != length:
            raise OptimizerUnavailable("optimizer_input_invalid")
        if any(not math.isfinite(value) for value in series):
            raise OptimizerUnavailable("optimizer_input_invalid")
        out[symbol] = series
    if length is None or length < REQUIRED_RETURNS:
        raise OptimizerUnavailable("optimizer_input_invalid")
    return out


def _returns_table(
    series_by_symbol: dict[str, tuple[float, ...]],
    symbols: tuple[str, ...],
) -> list[list[float]]:
    count = len(series_by_symbol[symbols[0]])
    rows: list[list[float]] = []
    for index in range(count):
        rows.append([float(series_by_symbol[symbol][index]) for symbol in symbols])
    return rows


def _as_float_weight(value: Any) -> float:
    """Convert one optimizer weight; malformed values are invalid weights."""
    if isinstance(value, (bool, str)):
        raise OptimizerUnavailable("optimizer_invalid_weights")
    if isinstance(value, (int, float)):
        weight = float(value)
    else:
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise OptimizerUnavailable("optimizer_invalid_weights") from exc
    if not math.isfinite(weight):
        raise OptimizerUnavailable("optimizer_invalid_weights")
    return weight


def _sanitize_weights(raw: Any, symbols: tuple[str, ...]) -> dict[str, float]:
    if raw is None:
        raise OptimizerUnavailable("optimizer_fit_failed")
    try:
        values = list(raw)
    except TypeError as exc:
        raise OptimizerUnavailable("optimizer_invalid_weights") from exc
    if len(values) != len(symbols):
        raise OptimizerUnavailable("optimizer_invalid_weights")
    tol = OPTIMIZER_NUMERICAL_TOLERANCE
    cleaned: list[float] = []
    for value in values:
        weight = _as_float_weight(value)
        if weight < -tol or weight > 1.0 + tol:
            raise OptimizerUnavailable("optimizer_invalid_weights")
        if weight <= tol:
            weight = 0.0
        elif weight >= 1.0 - tol:
            weight = 1.0
        cleaned.append(weight)
    total = sum(cleaned)
    if not math.isfinite(total) or total <= 0:
        raise OptimizerUnavailable("optimizer_budget_violation")
    if abs(total - 1.0) > tol:
        raise OptimizerUnavailable("optimizer_budget_violation")
    if total != 1.0:
        cleaned = [weight / total for weight in cleaned]
    out = {symbol: cleaned[index] for index, symbol in enumerate(symbols)}
    final_total = sum(out.values())
    if abs(final_total - 1.0) > WEIGHT_TOLERANCE:
        raise OptimizerUnavailable("optimizer_budget_violation")
    if any(weight < 0.0 or weight > 1.0 for weight in out.values()):
        raise OptimizerUnavailable("optimizer_invalid_weights")
    return out


def minimum_variance_weights(matrix: ReturnMatrix) -> dict[str, float]:
    """Return long-only minimum-variance weights keyed by canonical sorted symbols."""
    symbols = _canonical_symbols(matrix)
    series = _validated_series(matrix, symbols)
    model = mean_risk_minimum_variance_model()
    model.fit(_returns_table(series, symbols))
    raw = model.weights_
    if raw is None:
        diagnostic = getattr(model, "error_", None)
        text = str(diagnostic) if diagnostic is not None else None
        raise OptimizerUnavailable("optimizer_fit_failed", diagnostic=text)
    return _sanitize_weights(raw, symbols)
