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


def _validate_matrix(matrix: ReturnMatrix) -> None:
    if not matrix.symbols:
        raise OptimizerUnavailable("optimizer_input_invalid")
    length: int | None = None
    for symbol in matrix.symbols:
        series = matrix.returns.get(symbol)
        if series is None:
            raise OptimizerUnavailable("optimizer_input_invalid")
        if length is None:
            length = len(series)
        elif len(series) != length:
            raise OptimizerUnavailable("optimizer_input_invalid")
        if any(not math.isfinite(value) for value in series):
            raise OptimizerUnavailable("optimizer_input_invalid")
    if length is None or length < REQUIRED_RETURNS:
        raise OptimizerUnavailable("optimizer_input_invalid")


def _returns_table(matrix: ReturnMatrix) -> list[list[float]]:
    count = len(next(iter(matrix.returns.values())))
    rows: list[list[float]] = []
    for index in range(count):
        rows.append([float(matrix.returns[symbol][index]) for symbol in matrix.symbols])
    return rows


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
        if not math.isfinite(float(value)):
            raise OptimizerUnavailable("optimizer_invalid_weights")
        weight = float(value)
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
    """Return long-only minimum-variance weights keyed by ``matrix.symbols`` order."""
    _validate_matrix(matrix)
    model = mean_risk_minimum_variance_model()
    model.fit(_returns_table(matrix))
    raw = model.weights_
    if raw is None:
        diagnostic = getattr(model, "error_", None)
        text = str(diagnostic) if diagnostic is not None else None
        raise OptimizerUnavailable("optimizer_fit_failed", diagnostic=text)
    return _sanitize_weights(raw, matrix.symbols)
