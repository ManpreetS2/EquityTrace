"""Monthly and quarterly rebalance schedules (issue #24).

Every other backtest test uses ``PortfolioSchedule.EXPLICIT``. These tests execute
``monthly``/``quarterly`` through the calendar resolver and the engine, asserting the
decision session (signal knowledge) is strictly before the execution session.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from equitytrace.database import Database
from equitytrace.market.availability import bar_available_at
from equitytrace.portfolio.baselines import BaselineUnavailable, inverse_volatility
from equitytrace.portfolio.calendar import (
    CalendarError,
    CalendarSession,
    resolve_decision_sessions,
    resolve_rebalance_pairs,
)
from equitytrace.portfolio.models import (
    BacktestRequest,
    BacktestResult,
    PortfolioBaseline,
    PortfolioRunStatus,
    PortfolioSchedule,
    RebalanceStatus,
)
from equitytrace.portfolio.returns import ReturnMatrix
from helpers.portfolio_fixtures import (
    add_adjusted_bars,
    annual_roa_facts,
    run_backtest,
    seed_issuer,
    weekdays,
)

KNOWN = datetime(2023, 6, 1, tzinfo=UTC)
GOOD_FRIDAY = date(2024, 3, 29)  # stored calendar has no session this day


def _sessions(days: list[date]) -> list[CalendarSession]:
    return [
        CalendarSession(
            trading_date=day,
            available_at=bar_available_at(day, "America/New_York"),
            instrument_id="spy",
        )
        for day in days
    ]


def _calendar_days(start: date, end: date, *, holidays: set[date] = frozenset()) -> list[date]:  # type: ignore[assignment]
    out: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            out.append(current)
        current += timedelta(days=1)
    return out


# --- pure calendar resolution -------------------------------------------------


def test_monthly_decision_is_last_stored_session_and_effective_is_next() -> None:
    days = _calendar_days(date(2024, 1, 1), date(2024, 6, 30), holidays={GOOD_FRIDAY})
    pairs = resolve_rebalance_pairs(
        _sessions(days),
        schedule=PortfolioSchedule.MONTHLY,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 5, 31),
    )
    got = [(d.trading_date, e.trading_date) for d, e in pairs]
    assert (
        got
        == [
            (date(2024, 1, 31), date(2024, 2, 1)),
            (date(2024, 2, 29), date(2024, 3, 1)),
            # March 29 is not a stored session: decision falls back to Thursday the 28th.
            (date(2024, 3, 28), date(2024, 4, 1)),
            (date(2024, 4, 30), date(2024, 5, 1)),
            # May decision's next session (June 3) is after end_date: dropped, not executed.
        ][:4]
    )
    assert all(e.trading_date > d.trading_date for d, e in pairs)


def test_quarterly_only_march_june_september_december() -> None:
    days = _calendar_days(date(2023, 10, 2), date(2025, 1, 31))
    pairs = resolve_rebalance_pairs(
        _sessions(days),
        schedule=PortfolioSchedule.QUARTERLY,
        start_date=date(2023, 10, 2),
        end_date=date(2024, 12, 31),
    )
    decisions = [d.trading_date for d, _ in pairs]
    assert decisions == [
        date(2023, 12, 29),
        date(2024, 3, 28) + timedelta(days=1),  # 2024-03-29 stored here (no holiday set)
        date(2024, 6, 28),
        date(2024, 9, 30),
    ]
    assert {d.month for d in decisions} <= {3, 6, 9, 12}
    # Dec 2024: 12-31 is the window end, its next session (2025-01-01) is after end_date.
    assert date(2024, 12, 31) not in decisions


def test_year_boundary_pairs_december_with_january() -> None:
    days = _calendar_days(date(2023, 11, 1), date(2024, 2, 29))
    pairs = resolve_rebalance_pairs(
        _sessions(days),
        schedule=PortfolioSchedule.MONTHLY,
        start_date=date(2023, 11, 1),
        end_date=date(2024, 1, 31),
    )
    assert [(d.trading_date, e.trading_date) for d, e in pairs] == [
        (date(2023, 11, 30), date(2023, 12, 1)),
        (date(2023, 12, 29), date(2024, 1, 1)),
        # January's decision (the window end) would execute on Feb 1: dropped.
    ]


def test_decision_with_next_session_after_end_date_is_dropped() -> None:
    days = _calendar_days(date(2024, 1, 1), date(2024, 4, 15))
    # end_date is the last March session; its next session (April 1) is outside the window.
    pairs = resolve_rebalance_pairs(
        _sessions(days),
        schedule=PortfolioSchedule.MONTHLY,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 3, 29),
    )
    assert [d.trading_date for d, _ in pairs] == [date(2024, 1, 31), date(2024, 2, 29)]
    assert all(e.trading_date <= date(2024, 3, 29) for _, e in pairs)


def test_mid_month_end_date_uses_last_in_window_session() -> None:
    days = _calendar_days(date(2024, 1, 1), date(2024, 3, 29))
    window = dict(start_date=date(2024, 1, 2), end_date=date(2024, 2, 15))
    decisions = resolve_decision_sessions(
        _sessions(days), schedule=PortfolioSchedule.MONTHLY, **window
    )
    # February's "last session" is the in-window Feb 15, not Feb 29 beyond the window...
    assert [d.trading_date for d in decisions] == [date(2024, 1, 31), date(2024, 2, 15)]
    # ...but it is not executable because its next session (Feb 16) is after end_date.
    pairs = resolve_rebalance_pairs(_sessions(days), schedule=PortfolioSchedule.MONTHLY, **window)
    assert [d.trading_date for d, _ in pairs] == [date(2024, 1, 31)]


def test_start_mid_month_includes_that_months_end() -> None:
    days = _calendar_days(date(2024, 1, 1), date(2024, 3, 29))
    pairs = resolve_rebalance_pairs(
        _sessions(days),
        schedule=PortfolioSchedule.MONTHLY,
        start_date=date(2024, 1, 20),
        end_date=date(2024, 3, 15),
    )
    assert [d.trading_date for d, _ in pairs] == [date(2024, 1, 31), date(2024, 2, 29)]


def test_quarterly_window_without_quarter_end_month_is_unavailable() -> None:
    days = _calendar_days(date(2024, 1, 1), date(2024, 3, 29))
    with pytest.raises(CalendarError) as caught:
        resolve_rebalance_pairs(
            _sessions(days),
            schedule=PortfolioSchedule.QUARTERLY,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 2, 28),
        )
    assert caught.value.reason == "calendar_missing"


def test_no_sessions_is_calendar_missing() -> None:
    with pytest.raises(CalendarError):
        resolve_decision_sessions(
            [],
            schedule=PortfolioSchedule.MONTHLY,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
        )


# --- engine-level execution ---------------------------------------------------


def _seed(db: Database, days: list[date], *, calendar_through: date | None = None) -> None:
    """Seed AAA/BBB (valid ROA) plus SPY on a calendar that may extend past ``days``."""
    with db.session() as conn:
        for ticker, cik, income in (("AAA", "0001000001", 20.0), ("BBB", "0001000002", 10.0)):
            seed_issuer(
                conn,
                ticker=ticker,
                cik=cik,
                facts=annual_roa_facts(
                    cik=cik,
                    fiscal_year=2023,
                    net_income=income,
                    assets=100.0,
                    prior_assets=100.0,
                    available_at=KNOWN,
                    accession=f"{ticker.lower()}-2023",
                ),
            )
        calendar = [d for d in days if calendar_through is None or d <= calendar_through]
        add_adjusted_bars(conn, "SPY", [(d, 100.0) for d in calendar])
        add_adjusted_bars(
            conn, "AAA", [(d, 100.0 * (1.0 + 0.002 * math.sin(i / 3))) for i, d in enumerate(days)]
        )
        add_adjusted_bars(
            conn, "BBB", [(d, 50.0 * (1.0 + 0.003 * math.cos(i / 4))) for i, d in enumerate(days)]
        )


def _request(
    schedule: PortfolioSchedule,
    *,
    start: date,
    end: date,
    tickers: tuple[str, ...] = ("AAA", "BBB"),
) -> BacktestRequest:
    return BacktestRequest(
        tickers=tickers,
        factor="roa",
        top_n=2,
        baseline=PortfolioBaseline.EQUAL_WEIGHT,
        start_date=start,
        end_date=end,
        schedule=schedule,
        cost_bps=10.0,
        benchmark_symbol="SPY",
    )


def _days() -> list[date]:
    return _calendar_days(date(2024, 1, 2), date(2024, 10, 31), holidays={GOOD_FRIDAY})


def test_engine_monthly_decisions_execute_on_next_session(db: Database) -> None:
    days = _days()
    _seed(db, days)
    result = run_backtest(
        db, _request(PortfolioSchedule.MONTHLY, start=date(2024, 1, 2), end=date(2024, 6, 14))
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    pairs = [(r.decision_at.date(), r.target_effective_at.date()) for r in result.rebalances]
    assert pairs == [
        (date(2024, 1, 31), date(2024, 2, 1)),
        (date(2024, 2, 29), date(2024, 3, 1)),
        (date(2024, 3, 28), date(2024, 4, 1)),
        (date(2024, 4, 30), date(2024, 5, 1)),
        (date(2024, 5, 31), date(2024, 6, 3)),
    ]
    assert all(r.status is RebalanceStatus.SUCCESS for r in result.rebalances)
    for rebalance in result.rebalances:
        assert rebalance.decision_at < rebalance.target_effective_at
    # Trading happens at the effective session, never at the decision session.
    valuation_times = {point.valuation_at for point in result.equity}
    assert all(r.target_effective_at in valuation_times for r in result.rebalances)
    assert result.audit is not None
    assert result.audit.passed, result.audit.failures


def test_engine_drops_decision_whose_effective_session_is_after_end(db: Database) -> None:
    days = _days()
    # Stored calendar continues past end_date, so the June decision has a next session
    # (July 1) that is outside the window and must not execute.
    _seed(db, days)
    result = run_backtest(
        db, _request(PortfolioSchedule.MONTHLY, start=date(2024, 1, 2), end=date(2024, 6, 28))
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    decisions = [r.decision_at.date() for r in result.rebalances]
    assert date(2024, 6, 28) not in decisions
    assert decisions[-1] == date(2024, 5, 31)
    assert max(r.target_effective_at.date() for r in result.rebalances) <= date(2024, 6, 28)


def test_engine_quarterly_rebalances_only_in_quarter_end_months(db: Database) -> None:
    days = _days()
    _seed(db, days)
    result = run_backtest(
        db, _request(PortfolioSchedule.QUARTERLY, start=date(2024, 1, 2), end=date(2024, 10, 15))
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    pairs = [(r.decision_at.date(), r.target_effective_at.date()) for r in result.rebalances]
    assert pairs == [
        (date(2024, 3, 28), date(2024, 4, 1)),
        (date(2024, 6, 28), date(2024, 7, 1)),
        (date(2024, 9, 30), date(2024, 10, 1)),
    ]


def test_engine_schedule_without_executable_decision_is_unavailable(db: Database) -> None:
    days = _days()
    _seed(db, days)
    result = run_backtest(
        db, _request(PortfolioSchedule.QUARTERLY, start=date(2024, 1, 2), end=date(2024, 2, 28))
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "calendar_missing"
    assert result.rebalances == ()


def _comparable(result: BacktestResult) -> dict[str, object]:
    return {
        "status": result.status,
        "rebalances": [r.model_dump(mode="json") for r in result.rebalances],
        "equity": [p.model_dump(mode="json") for p in result.equity],
        "metrics": result.metrics.model_dump(mode="json") if result.metrics else None,
        "final_nav": result.final_nav,
    }


@pytest.mark.parametrize("schedule", [PortfolioSchedule.MONTHLY, PortfolioSchedule.QUARTERLY])
def test_scheduled_backtest_is_deterministic(db: Database, schedule: PortfolioSchedule) -> None:
    _seed(db, _days())
    window = dict(start=date(2024, 1, 2), end=date(2024, 10, 15))
    first = run_backtest(db, _request(schedule, **window))
    second = run_backtest(db, _request(schedule, **window))
    reordered = run_backtest(db, _request(schedule, tickers=("BBB", "aaa"), **window))
    assert first.status is PortfolioRunStatus.SUCCESS
    assert _comparable(first) == _comparable(second)
    assert _comparable(first) == _comparable(reordered)


def test_monthly_backtest_is_deterministic_under_hash_seed(db: Database) -> None:
    """Listed in the CI determinism matrix (PYTHONHASHSEED 0/1/42)."""
    _seed(db, _days())
    window = dict(start=date(2024, 1, 2), end=date(2024, 10, 15))
    first = run_backtest(db, _request(PortfolioSchedule.MONTHLY, **window), persist=False)
    second = run_backtest(
        db, _request(PortfolioSchedule.MONTHLY, tickers=("BBB", "AAA"), **window), persist=False
    )
    assert first.status is PortfolioRunStatus.SUCCESS
    assert _comparable(first) == _comparable(second)
    assert len(weekdays(date(2024, 1, 1), 3)) == 3


# --- inverse-volatility guards ------------------------------------------------


@pytest.mark.parametrize(
    "series",
    [
        (0.01,) * 30,  # zero variance
        (0.01, math.nan, 0.02, 0.03),  # non-finite stdev
        (0.01, math.inf, 0.02, 0.03),
        (0.01,),  # too short
    ],
)
def test_inverse_volatility_rejects_degenerate_series(series: tuple[float, ...]) -> None:
    matrix = ReturnMatrix(
        symbols=("AAA", "BBB"),
        dates=(),
        returns={"AAA": series, "BBB": (0.01, -0.01, 0.02, -0.02)},
    )
    with pytest.raises(BaselineUnavailable):
        inverse_volatility(matrix)


def test_inverse_volatility_weights_sum_to_one_and_favor_low_vol() -> None:
    matrix = ReturnMatrix(
        symbols=("AAA", "BBB"),
        dates=(),
        returns={"AAA": (0.01, -0.01, 0.01, -0.01), "BBB": (0.04, -0.04, 0.04, -0.04)},
    )
    weights = inverse_volatility(matrix)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["AAA"] > weights["BBB"]
