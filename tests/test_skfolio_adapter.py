"""skfolio minimum-variance adapter integration tests."""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from skfolio import RiskMeasure
from skfolio.optimization import MeanRisk, ObjectiveFunction

from equitytrace.portfolio.models import REQUIRED_RETURNS, WEIGHT_TOLERANCE
from equitytrace.portfolio.returns import ReturnMatrix
from equitytrace.portfolio.skfolio_adapter import (
    OPTIMIZER_NUMERICAL_TOLERANCE,
    OptimizerUnavailable,
    mean_risk_minimum_variance_model,
    minimum_variance_weights,
)


def _synthetic_matrix(*, symbol_order: tuple[str, ...] | None = None) -> ReturnMatrix:
    symbols = symbol_order or ("AAA", "BBB")
    by_symbol: dict[str, list[float]] = {}
    for label, scale in (("AAA", 0.001), ("BBB", 0.002)):
        series: list[float] = []
        for index in range(REQUIRED_RETURNS):
            wave = math.sin(index / 17.0) if label == "AAA" else math.cos(index / 23.0)
            series.append(scale * wave)
        by_symbol[label] = series
    return ReturnMatrix(
        symbols=symbols,
        dates=tuple(date(2020, 1, 1) for _ in range(REQUIRED_RETURNS + 1)),
        returns={symbol: tuple(by_symbol[symbol]) for symbol in symbols},
    )


def test_mean_risk_configuration() -> None:
    model = mean_risk_minimum_variance_model()
    assert model.objective_function is ObjectiveFunction.MINIMIZE_RISK
    assert model.risk_measure is RiskMeasure.VARIANCE
    assert model.min_weights == 0.0
    assert model.max_weights == 1.0
    assert model.budget == 1.0
    assert model.solver == "CLARABEL"
    assert model.transaction_costs == 0.0
    assert model.management_fees == 0.0
    assert model.fallback is None
    assert model.raise_on_failure is False
    assert model.previous_weights is None
    assert model.max_turnover is None


def test_real_minimum_variance_solve() -> None:
    matrix = _synthetic_matrix()
    weights = minimum_variance_weights(matrix)
    assert set(weights) == set(matrix.symbols)
    for symbol in matrix.symbols:
        value = weights[symbol]
        assert math.isfinite(value)
        assert 0.0 <= value <= 1.0
    assert sum(weights.values()) == pytest.approx(1.0, abs=WEIGHT_TOLERANCE)


def test_order_determinism() -> None:
    forward = minimum_variance_weights(_synthetic_matrix(symbol_order=("AAA", "BBB")))
    reverse = minimum_variance_weights(_synthetic_matrix(symbol_order=("BBB", "AAA")))
    assert forward["AAA"] == pytest.approx(reverse["AAA"], abs=2e-5)
    assert forward["BBB"] == pytest.approx(reverse["BBB"], abs=2e-5)


def test_fit_failure_surfaces_without_fallback() -> None:
    matrix = _synthetic_matrix()
    model = MagicMock(spec=MeanRisk)
    model.weights_ = None
    model.error_ = "solver failed"
    with (
        patch(
            "equitytrace.portfolio.skfolio_adapter.mean_risk_minimum_variance_model",
            return_value=model,
        ),
        pytest.raises(OptimizerUnavailable) as exc,
    ):
        minimum_variance_weights(matrix)
    assert exc.value.reason == "optimizer_fit_failed"
    assert exc.value.diagnostic == "solver failed"
    model.fit.assert_called_once()


@pytest.mark.parametrize(
    ("weights", "reason"),
    [
        ([float("nan"), 0.5], "optimizer_invalid_weights"),
        ([-0.5, 1.5], "optimizer_invalid_weights"),
        ([0.4, 0.4], "optimizer_budget_violation"),
        ([0.5], "optimizer_invalid_weights"),
    ],
)
def test_invalid_optimizer_output(weights: list[float], reason: str) -> None:
    matrix = _synthetic_matrix()
    model = MagicMock(spec=MeanRisk)
    model.weights_ = weights
    model.error_ = None
    with (
        patch(
            "equitytrace.portfolio.skfolio_adapter.mean_risk_minimum_variance_model",
            return_value=model,
        ),
        pytest.raises(OptimizerUnavailable) as exc,
    ):
        minimum_variance_weights(matrix)
    assert exc.value.reason == reason


def test_input_validation_rejects_short_history() -> None:
    matrix = ReturnMatrix(
        symbols=("AAA",),
        dates=(),
        returns={"AAA": (0.01, 0.02)},
    )
    with pytest.raises(OptimizerUnavailable) as exc:
        minimum_variance_weights(matrix)
    assert exc.value.reason == "optimizer_input_invalid"


def test_numerical_tolerance_clamps_near_bounds() -> None:
    tol = OPTIMIZER_NUMERICAL_TOLERANCE
    matrix = _synthetic_matrix()
    model = MagicMock(spec=MeanRisk)
    model.weights_ = [tol / 10.0, 1.0 - tol / 10.0]
    model.error_ = None
    with patch(
        "equitytrace.portfolio.skfolio_adapter.mean_risk_minimum_variance_model",
        return_value=model,
    ):
        weights = minimum_variance_weights(matrix)
    assert weights["AAA"] == pytest.approx(0.0, abs=WEIGHT_TOLERANCE)
    assert weights["BBB"] == pytest.approx(1.0, abs=WEIGHT_TOLERANCE)
