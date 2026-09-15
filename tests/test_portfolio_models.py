"""Request/status models and weight-accounting primitives."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from equitytrace.portfolio.engine import (
    PortfolioState,
    apply_interval_returns,
    apply_weight_transition,
    assert_weights,
    gross_turnover,
)
from equitytrace.portfolio.models import (
    WEIGHT_TOLERANCE,
    BacktestRequest,
    PortfolioBaseline,
    PortfolioSchedule,
)


def test_request_rejects_invalid_top_n() -> None:
    with pytest.raises(ValidationError):
        BacktestRequest(
            tickers=("AAA",),
            factor="roa",
            top_n=0,
            baseline=PortfolioBaseline.EQUAL_WEIGHT,
            start_date=__import__("datetime").date(2024, 1, 1),
            end_date=__import__("datetime").date(2024, 2, 1),
            schedule=PortfolioSchedule.MONTHLY,
        )


def test_request_requires_adjustment_mode_all() -> None:
    from datetime import date

    from equitytrace.market.models import PriceAdjustmentMode

    kwargs = {
        "tickers": ("AAA",),
        "factor": "roa",
        "top_n": 1,
        "baseline": PortfolioBaseline.EQUAL_WEIGHT,
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 2, 1),
        "schedule": PortfolioSchedule.MONTHLY,
    }
    accepted = BacktestRequest(**kwargs)
    assert accepted.adjustment_mode is PriceAdjustmentMode.ALL
    for mode in (
        PriceAdjustmentMode.NONE,
        PriceAdjustmentMode.SPLITS,
        PriceAdjustmentMode.DIVIDENDS,
    ):
        with pytest.raises(ValidationError, match="adjustment_mode=all"):
            BacktestRequest(**kwargs, adjustment_mode=mode)


def test_explicit_schedule_requires_dates() -> None:
    from datetime import date

    with pytest.raises(ValidationError):
        BacktestRequest(
            tickers=("AAA",),
            factor="roa",
            top_n=1,
            baseline=PortfolioBaseline.EQUAL_WEIGHT,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
            schedule=PortfolioSchedule.EXPLICIT,
        )


def test_gross_turnover_treats_missing_names_as_zero() -> None:
    assert gross_turnover({}, {"AAA": 0.5, "BBB": 0.5}) == pytest.approx(1.0)
    assert gross_turnover({"AAA": 0.5, "BBB": 0.5}, {"AAA": 1.0}) == pytest.approx(1.0)


def test_weight_sum_tolerance_after_transition() -> None:
    state = PortfolioState(nav=1.0, cash_weight=1.0, asset_weights={})
    apply_weight_transition(
        state,
        {"AAA": 0.5, "BBB": 0.5},
        target_cash=0.0,
        cost_rate=0.001,
    )
    assert_weights(state)
    total = state.cash_weight + sum(state.asset_weights.values())
    assert abs(total - 1.0) <= WEIGHT_TOLERANCE


def test_apply_returns_requires_held_interval() -> None:
    from equitytrace.portfolio.engine import PortfolioFailed

    state = PortfolioState(nav=1.0, cash_weight=0.0, asset_weights={"AAA": 1.0})
    with pytest.raises(PortfolioFailed, match="held_return_missing"):
        apply_interval_returns(state, {})
