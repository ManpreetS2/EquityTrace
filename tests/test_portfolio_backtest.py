"""Accounting, selection, calendar, and engine regressions for native backtests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from equitytrace.database import Database
from equitytrace.financials.service import FinancialsService
from equitytrace.market.models import PriceAdjustmentMode
from equitytrace.portfolio.baselines import equal_weight, inverse_volatility
from equitytrace.portfolio.calendar import (
    CalendarError,
    CalendarSession,
    resolve_rebalance_pairs,
)
from equitytrace.portfolio.engine import (
    PortfolioState,
    apply_interval_returns,
    apply_weight_transition,
    gross_turnover,
)
from equitytrace.portfolio.models import (
    MIXED_PERIODS_WARNING,
    SAME_ISSUER_UNAVAILABLE,
    PortfolioRunStatus,
    PortfolioSchedule,
    RebalanceStatus,
)
from equitytrace.portfolio.returns import ReturnMatrix, aligned_return_matrix
from equitytrace.portfolio.signals import normalize_universe, tickers_sharing_an_issuer
from equitytrace.repositories.portfolio import PortfolioRepository
from helpers.financial_fixtures import make_fact
from helpers.portfolio_fixtures import (
    add_adjusted_bars,
    add_share_class,
    annual_fcf_facts,
    annual_roa_facts,
    explicit_request,
    run_backtest,
    seed_issuer,
    weekdays,
)

D0 = date(2024, 1, 2)
D1 = date(2024, 1, 3)
D2 = date(2024, 1, 4)
D3 = date(2024, 1, 5)
KNOWN = datetime(2023, 6, 1, tzinfo=UTC)


def test_weights_drift_before_next_rebalance() -> None:
    state = PortfolioState(nav=1.0, cash_weight=1.0, asset_weights={})
    first = apply_weight_transition(
        state,
        {"AAA": 0.5, "BBB": 0.5},
        target_cash=0.0,
        cost_rate=0.001,
    )
    assert first.gross_turnover == pytest.approx(1.0)
    assert first.cost_amount == pytest.approx(0.001)
    assert state.nav == pytest.approx(0.999)
    apply_interval_returns(state, {"AAA": 0.10, "BBB": 0.0})
    assert state.nav == pytest.approx(1.04895)
    assert state.asset_weights["AAA"] == pytest.approx(11.0 / 21.0)
    assert state.asset_weights["BBB"] == pytest.approx(10.0 / 21.0)
    drifted = dict(state.asset_weights)
    stale_target_turnover = gross_turnover({"AAA": 0.5, "BBB": 0.5}, {"AAA": 1.0, "BBB": 0.0})
    second = apply_weight_transition(
        state,
        {"AAA": 1.0, "BBB": 0.0},
        target_cash=0.0,
        cost_rate=0.001,
    )
    assert second.gross_turnover == pytest.approx(20.0 / 21.0)
    assert second.gross_turnover != pytest.approx(stale_target_turnover)
    assert gross_turnover(drifted, {"AAA": 1.0, "BBB": 0.0}) == pytest.approx(20.0 / 21.0)
    assert second.cost_amount == pytest.approx(0.000999)
    assert state.nav == pytest.approx(1.047951)
    apply_interval_returns(state, {"AAA": -0.05})
    assert state.nav == pytest.approx(0.99555345)
    # Peak is the pre-second-transition mark 1.04895, not the post-cost NAV.
    assert pytest.approx(-0.0509047619) == 0.99555345 / 1.04895 - 1


def test_cash_drift_without_rebalance() -> None:
    state = PortfolioState(nav=1.0, cash_weight=0.5, asset_weights={"AAA": 0.5})
    apply_interval_returns(state, {"AAA": 0.20})
    assert state.nav == pytest.approx(1.1)
    assert state.asset_weights["AAA"] == pytest.approx(6.0 / 11.0)
    assert state.cash_weight == pytest.approx(5.0 / 11.0)


def test_pre_transition_mark_is_drawdown_peak(db: Database) -> None:
    _seed_two_name_path(db, aaa_closes=(100.0, 100.0, 110.0, 104.5))
    result = run_backtest(
        db,
        explicit_request(
            ("AAA", "BBB"),
            start=D0,
            end=D3,
            decisions=(D0, D1),
            top_n=2,
        ),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    assert result.metrics is not None
    assert result.metrics.max_drawdown is not None
    highs = [row.pre_cost_nav for row in result.rebalances if row.status is RebalanceStatus.SUCCESS]
    assert any(nav == pytest.approx(1.04895) for nav in highs)
    assert result.metrics.max_drawdown == pytest.approx((result.final_nav or 0.0) / 1.04895 - 1.0)


def test_equity_points_are_post_event_and_preserve_pre_cost_peak(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1), top_n=2),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    assert result.equity
    first = result.equity[0]
    assert first.nav == pytest.approx(1.0)
    assert first.cash_weight == pytest.approx(1.0)
    assert first.drawdown == pytest.approx(0.0)
    success = [row for row in result.rebalances if row.status is RebalanceStatus.SUCCESS]
    by_time = {point.valuation_at: point for point in result.equity}
    assert len(by_time) == len(result.equity)
    peak = 1.0
    for reb in success:
        point = by_time[reb.target_effective_at]
        assert point.nav == pytest.approx(reb.post_cost_nav)
        assert point.cash_weight == pytest.approx(0.0)
        peak = max(peak, reb.pre_cost_nav)
        assert point.drawdown == pytest.approx(point.nav / peak - 1.0)
        assert point.nav != pytest.approx(reb.pre_cost_nav) or reb.cost_amount == 0.0
    assert any(row.pre_cost_nav == pytest.approx(1.04895) for row in success)
    assert result.metrics is not None
    assert result.metrics.max_drawdown == pytest.approx((result.final_nav or 0.0) / 1.04895 - 1.0)
    assert result.equity[-1].nav == pytest.approx(result.final_nav or 0.0)
    with db.session() as conn:
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert len(loaded.equity) == len(result.equity)
    for got, expected in zip(loaded.equity, result.equity, strict=True):
        assert got.valuation_at == expected.valuation_at
        assert got.nav == pytest.approx(expected.nav)
        assert got.cash_weight == pytest.approx(expected.cash_weight)
        assert got.drawdown == pytest.approx(expected.drawdown)


def _seed_two_name_path(
    db: Database,
    *,
    aaa_closes: tuple[float, float, float, float] = (100.0, 100.0, 110.0, 104.5),
    bbb_closes: tuple[float, float, float, float] = (100.0, 100.0, 100.0, 100.0),
    skip_bbb: date | None = None,
) -> None:
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        seed_issuer(
            conn,
            ticker="BBB",
            cik="0001000002",
            facts=annual_roa_facts(
                cik="0001000002",
                fiscal_year=2023,
                net_income=10.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="bbb-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", list(zip(days, aaa_closes, strict=True)))
        skip = {skip_bbb} if skip_bbb else set()
        add_adjusted_bars(
            conn,
            "BBB",
            list(zip(days, bbb_closes, strict=True)),
            skip_dates=skip,
        )


def test_engine_equal_weight_records_pre_cost_high_and_drifted_turnover(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1), top_n=2),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    success = [row for row in result.rebalances if row.status is RebalanceStatus.SUCCESS]
    assert success[0].cost_amount == pytest.approx(0.001)
    assert success[0].post_cost_nav == pytest.approx(0.999)
    assert success[1].pre_cost_nav == pytest.approx(1.04895)
    drifted_turnover = abs(0.5 - 11.0 / 21.0) + abs(0.5 - 10.0 / 21.0)
    assert success[1].gross_turnover == pytest.approx(drifted_turnover)
    assert success[1].gross_turnover != pytest.approx(0.0)
    assert result.metrics is not None
    assert result.metrics.max_drawdown == pytest.approx((result.final_nav or 0.0) / 1.04895 - 1.0)


def test_held_return_missing_fails_run(db: Database) -> None:
    _seed_two_name_path(db, skip_bbb=D2)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.FAILED
    assert result.failure_reason == "held_return_missing"


def test_first_rebalance_unavailable_when_universe_too_small(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA",), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "factor_universe_too_small"
    assert MIXED_PERIODS_WARNING not in result.warnings


def test_later_target_support_missing_holds_drifted_state(db: Database) -> None:
    days = [D0, D1, D2, D3]
    late = datetime(2024, 1, 3, 12, 0, tzinfo=UTC)
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=5.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        seed_issuer(
            conn,
            ticker="CCC",
            cik="0001000003",
            facts=annual_roa_facts(
                cik="0001000003",
                fiscal_year=2023,
                net_income=50.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=late,
                accession="ccc-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "CCC", [(day, 100.0) for day in days], skip_dates={D2})
    result = run_backtest(
        db,
        explicit_request(("AAA", "CCC"), start=D0, end=D3, decisions=(D0, D1), top_n=1),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    statuses = [row.status for row in result.rebalances]
    assert RebalanceStatus.SUCCESS in statuses
    assert RebalanceStatus.UNAVAILABLE in statuses
    later = next(row for row in result.rebalances if row.status is RebalanceStatus.UNAVAILABLE)
    assert later.reason == "target_support_missing"
    assert later.gross_turnover == 0.0
    assert later.cost_amount == 0.0


def test_first_target_support_missing_makes_run_unavailable(db: Database) -> None:
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days], skip_dates={D1})
    result = run_backtest(
        db,
        explicit_request(("AAA",), start=D0, end=D3, decisions=(D0,), top_n=1),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "target_support_missing"


def test_latest_fy_no_silent_fallback(db: Database) -> None:
    decision = datetime(2024, 6, 1, tzinfo=UTC)
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="DDD",
            cik="0001000004",
            facts=annual_roa_facts(
                cik="0001000004",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="ddd-2023",
            )
            + annual_roa_facts(
                cik="0001000004",
                fiscal_year=2024,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="ddd-2024",
            ),
        )
        # Replace FY2024 net income with a missing-income year by seeding only assets? latest
        # FY2024 has NI so it is eligible. Seed a company whose latest FY lacks NI.
        seed_issuer(
            conn,
            ticker="EEE",
            cik="0001000005",
            facts=[
                *annual_roa_facts(
                    cik="0001000005",
                    fiscal_year=2023,
                    net_income=15.0,
                    assets=100.0,
                    prior_assets=100.0,
                    available_at=KNOWN,
                    accession="eee-2023",
                ),
                make_fact(
                    cik="0001000005",
                    concept="Assets",
                    value=100.0,
                    start=None,
                    end=date(2024, 12, 31),
                    available_at=KNOWN,
                    accession="eee-2024",
                    form="10-K",
                    fiscal_year=2024,
                    fiscal_period="FY",
                ),
            ],
        )
        service = FinancialsService(conn)
        years_d = [p.fiscal_year for p in service.list_available_annual_periods("DDD", decision)]
        years_e = [p.fiscal_year for p in service.list_available_annual_periods("EEE", decision)]
    assert years_d[0] == 2024
    assert years_e[0] == 2024
    from equitytrace.factors.engine import FactorEngine
    from equitytrace.portfolio.signals import resolve_latest_annual_factor

    with db.session() as conn:
        engine = FactorEngine(conn)
        result, reason, _ = resolve_latest_annual_factor(engine, "EEE", "roa", decision)
    assert result is None
    assert reason is not None


def test_mixed_fiscal_periods_warning(db: Database) -> None:
    days = [D0, D1, D2, D3]
    fy24 = datetime(2024, 1, 1, tzinfo=UTC)
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=12.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        seed_issuer(
            conn,
            ticker="BBB",
            cik="0001000002",
            facts=annual_roa_facts(
                cik="0001000002",
                fiscal_year=2024,
                net_income=18.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=fy24,
                accession="bbb-2024",
            ),
        )
        seed_issuer(
            conn,
            ticker="CCC",
            cik="0001000003",
            facts=annual_roa_facts(
                cik="0001000003",
                fiscal_year=2023,
                net_income=8.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="ccc-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        for ticker in ("AAA", "BBB", "CCC"):
            add_adjusted_bars(conn, ticker, [(day, 100.0) for day in days])
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB", "CCC"), start=D0, end=D3, decisions=(D0,), top_n=3),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    assert MIXED_PERIODS_WARNING in result.warnings
    periods = {
        row.signal_period
        for reb in result.rebalances
        for row in reb.transitions
        if row.signal_period
    }
    assert "FY2023" in periods
    assert "FY2024" in periods


def test_mixed_fiscal_periods_warning_survives_unavailable_target_support(db: Database) -> None:
    days = [D0, D1, D2, D3]
    fy24 = datetime(2024, 1, 3, 12, 0, tzinfo=UTC)
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            )
            + annual_roa_facts(
                cik="0001000001",
                fiscal_year=2024,
                net_income=18.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=fy24,
                accession="aaa-2024",
            ),
        )
        seed_issuer(
            conn,
            ticker="BBB",
            cik="0001000002",
            facts=annual_roa_facts(
                cik="0001000002",
                fiscal_year=2023,
                net_income=10.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="bbb-2023",
            ),
        )
        seed_issuer(
            conn,
            ticker="CCC",
            cik="0001000003",
            facts=annual_roa_facts(
                cik="0001000003",
                fiscal_year=2023,
                net_income=50.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=fy24,
                accession="ccc-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "BBB", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "CCC", [(day, 100.0) for day in days], skip_dates={D2})
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB", "CCC"), start=D0, end=D3, decisions=(D0, D1), top_n=2),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    success = [row for row in result.rebalances if row.status is RebalanceStatus.SUCCESS]
    later = [row for row in result.rebalances if row.status is RebalanceStatus.UNAVAILABLE]
    assert len(success) == 1
    first_held = {row.symbol for row in success[0].transitions if row.target_weight_after > 0}
    assert first_held == {"AAA", "BBB"}
    assert later
    assert later[0].reason == "target_support_missing"
    assert later[0].gross_turnover == 0.0
    assert later[0].cost_amount == 0.0
    assert later[0].transitions == ()
    assert later[0].post_cost_nav == pytest.approx(later[0].pre_cost_nav)
    assert MIXED_PERIODS_WARNING in result.warnings


def test_valuation_signal_provenance_keeps_market_input(db: Database) -> None:
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_fcf_facts(
                cik="0001000001",
                fiscal_year=2023,
                operating_cash_flow=40.0,
                capex=-10.0,
                shares=10.0,
                available_at=KNOWN,
                accession="aaa-fcf-2023",
                shares_accession="aaa-shares-2023",
            ),
        )
        seed_issuer(
            conn,
            ticker="BBB",
            cik="0001000002",
            facts=annual_fcf_facts(
                cik="0001000002",
                fiscal_year=2023,
                operating_cash_flow=20.0,
                capex=-5.0,
                shares=10.0,
                available_at=KNOWN,
                accession="bbb-fcf-2023",
                shares_accession="bbb-shares-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        for ticker, cik in (("AAA", "0001000001"), ("BBB", "0001000002")):
            points = [(day, 30.0) for day in days]
            add_adjusted_bars(conn, ticker, points, issuer_cik=cik)
            add_adjusted_bars(
                conn,
                ticker,
                points,
                issuer_cik=cik,
                adjustment_mode=PriceAdjustmentMode.NONE,
            )
    result = run_backtest(
        db,
        explicit_request(
            ("AAA", "BBB"),
            start=D0,
            end=D3,
            decisions=(D0,),
            top_n=1,
            factor="fcf_yield",
        ),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    selected = [
        row for reb in result.rebalances for row in reb.transitions if row.target_weight_after > 0
    ]
    assert selected
    payload = json.loads(selected[0].signal_provenance_json or "{}")
    assert payload["factor"] == "fcf_yield"
    assert payload["ticker"] == "AAA"
    assert payload["cik"] == "0001000001"
    assert payload["period"] == "FY2023"
    assert payload["as_of"]
    assert payload["inputs"]["free_cash_flow"] is not None
    assert "aaa-fcf-2023" in payload["source_filings"]
    market = payload["market_input"]
    assert market is not None
    assert market["price_adjustment_mode"] == PriceAdjustmentMode.NONE.value
    assert market["raw_close"] is not None
    assert market["shares_outstanding"] is not None
    assert market["sec_accession"] == "aaa-shares-2023"
    assert market["sec_concept"] == "CommonStockSharesOutstanding"
    assert market["sec_form"] == "10-K"
    with db.session() as conn:
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    stored = json.loads(loaded.rebalances[0].transitions[0].signal_provenance_json or "{}")
    assert stored["market_input"]["price_adjustment_mode"] == "none"
    assert stored["market_input"]["sec_accession"] == "aaa-shares-2023"


def test_universe_order_and_mixed_case_duplicates_are_deterministic(db: Database) -> None:
    _seed_two_name_path(db)
    a = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1)),
    )
    b = run_backtest(
        db,
        explicit_request(("bbb", "AAA", "aaa"), start=D0, end=D3, decisions=(D0, D1)),
    )
    assert a.status is PortfolioRunStatus.SUCCESS
    assert b.final_nav == pytest.approx(a.final_nav or 0)
    assert [row.symbol for row in a.rebalances[0].transitions] == [
        row.symbol for row in b.rebalances[0].transitions
    ]


def test_top_n_cutoff_does_not_include_extra_ties(db: Database) -> None:
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        for ticker, cik, income in (
            ("AAA", "0001000001", 10.0),
            ("BBB", "0001000002", 10.0),
            ("CCC", "0001000003", 10.0),
        ):
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
                    accession=f"{ticker}-2023",
                ),
            )
            add_adjusted_bars(conn, ticker, [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
    result = run_backtest(
        db,
        explicit_request(("CCC", "BBB", "AAA"), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    held = [row.symbol for row in result.rebalances[0].transitions if row.target_weight_after > 0]
    assert held == ["AAA", "BBB"]


def test_equal_weight_no_names_unavailable() -> None:
    from equitytrace.portfolio.baselines import BaselineUnavailable

    with pytest.raises(BaselineUnavailable):
        equal_weight([])


def test_inverse_vol_requires_common_history() -> None:
    from equitytrace.portfolio.baselines import BaselineUnavailable

    with pytest.raises(BaselineUnavailable):
        inverse_volatility(
            ReturnMatrix(
                symbols=("AAA",),
                dates=(),
                returns={"AAA": (0.0,) * 252},
            )
        )


def test_inverse_vol_interior_hole_is_not_forward_filled() -> None:

    from equitytrace.market.availability import bar_available_at
    from equitytrace.market.models import DailyPriceBar, MarketDataProviderName, PriceAdjustmentMode

    start = date(2023, 1, 2)
    days = weekdays(start, 260)
    as_of = datetime(2024, 12, 31, tzinfo=UTC)

    def bars_for(skip: date | None) -> list[DailyPriceBar]:
        out = []
        for day in days:
            if skip is not None and day == skip:
                continue
            close = Decimal("10")
            out.append(
                DailyPriceBar(
                    instrument_id="x",
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=day,
                    adjustment_mode=PriceAdjustmentMode.ALL,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1,
                    available_at=bar_available_at(day, "America/New_York"),
                    fetched_at=as_of,
                )
            )
        return out

    hole = days[100]
    matrix = aligned_return_matrix(
        {"AAA": bars_for(None), "BBB": bars_for(hole)},
        as_of=as_of,
        session_date=days[-1],
        required_returns=252,
    )
    # 260 vs 259 common dates still >= 253 closes.
    assert matrix is not None
    short = aligned_return_matrix(
        {"AAA": bars_for(None)[:252], "BBB": bars_for(None)[:252]},
        as_of=as_of,
        session_date=days[-1],
        required_returns=252,
    )
    assert short is None
    zeroed = bars_for(None)
    zeroed[10] = zeroed[10].model_copy(update={"close": Decimal("0")})
    assert (
        aligned_return_matrix(
            {"AAA": zeroed},
            as_of=as_of,
            session_date=days[-1],
            required_returns=10,
        )
        is None
    )


def test_normalize_universe_first_wins() -> None:
    assert normalize_universe(("aaa", "BBB", "AAA", "bbb")) == ["AAA", "BBB"]


def test_tickers_sharing_an_issuer_does_not_pick_a_class() -> None:
    shared = tickers_sharing_an_issuer(
        {"GOOG": "0001652044", "GOOGL": "0001652044", "MSFT": "0000789019"}
    )
    assert shared == {"0001652044": ("GOOG", "GOOGL")}
    assert tickers_sharing_an_issuer({"AAA": "1", "BBB": "2"}) == {}


def test_same_issuer_two_securities_are_unavailable(db: Database) -> None:
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        add_share_class(conn, ticker="AAB", cik="0001000001")
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days], issuer_cik="0001000001")
        add_adjusted_bars(conn, "AAB", [(day, 110.0) for day in days], issuer_cik="0001000001")
    result = run_backtest(
        db,
        explicit_request(("AAA", "AAB"), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == SAME_ISSUER_UNAVAILABLE
    assert result.request.tickers == ("AAA", "AAB")
    assert result.universe == ("AAA", "AAB")
    assert result.rebalances == ()
    assert result.final_nav is None
    assert result.metrics is not None
    assert result.metrics.transaction_cost_total == 0.0
    assert result.metrics.successful_rebalance_count == 0


def test_same_issuer_is_unavailable_even_for_top_n_one(db: Database) -> None:
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        add_share_class(conn, ticker="AAB", cik="0001000001")
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days], issuer_cik="0001000001")
        add_adjusted_bars(conn, "AAB", [(day, 100.0) for day in days], issuer_cik="0001000001")
    result = run_backtest(
        db,
        explicit_request(("AAB", "AAA"), start=D0, end=D3, decisions=(D0,), top_n=1),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == SAME_ISSUER_UNAVAILABLE
    assert result.request.tickers == ("AAB", "AAA")


def test_distinct_issuers_remain_available(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    held = {
        row.symbol
        for reb in result.rebalances
        for row in reb.transitions
        if row.target_weight_after > 0
    }
    assert held == {"AAA", "BBB"}


def _calendar(days: tuple[date, ...]) -> list[CalendarSession]:
    return [
        CalendarSession(
            trading_date=day,
            available_at=datetime(day.year, day.month, day.day, 21, 15, tzinfo=UTC),
            instrument_id="cal",
        )
        for day in days
    ]


def test_explicit_pairs_reject_out_of_range_and_missing_effective() -> None:
    sessions = _calendar((D0, D1, D2, D3))
    with pytest.raises(CalendarError) as before_start:
        resolve_rebalance_pairs(
            sessions,
            schedule=PortfolioSchedule.EXPLICIT,
            start_date=D0,
            end_date=D3,
            explicit_dates=(date(2023, 12, 29),),
        )
    assert before_start.value.reason == "explicit_date_out_of_range"
    with pytest.raises(CalendarError) as after_end:
        resolve_rebalance_pairs(
            sessions,
            schedule=PortfolioSchedule.EXPLICIT,
            start_date=D0,
            end_date=D3,
            explicit_dates=(date(2024, 1, 8),),
        )
    assert after_end.value.reason == "explicit_date_out_of_range"
    with pytest.raises(CalendarError) as no_next:
        resolve_rebalance_pairs(
            sessions,
            schedule=PortfolioSchedule.EXPLICIT,
            start_date=D0,
            end_date=D3,
            explicit_dates=(D3,),
        )
    assert no_next.value.reason == "calendar_missing"
    with pytest.raises(CalendarError) as effective_after:
        resolve_rebalance_pairs(
            sessions,
            schedule=PortfolioSchedule.EXPLICIT,
            start_date=D0,
            end_date=D1,
            explicit_dates=(D1,),
        )
    assert effective_after.value.reason == "effective_session_out_of_range"


def test_explicit_date_before_start_is_unavailable(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(date(2024, 1, 1),), top_n=2),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "explicit_date_out_of_range"
    assert result.rebalances == ()


def test_explicit_date_after_end_is_unavailable(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(date(2024, 1, 8),), top_n=2),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "explicit_date_out_of_range"


def test_explicit_last_session_without_next_is_unavailable(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D3,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "calendar_missing"


def test_explicit_effective_after_end_is_unavailable(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D1, decisions=(D1,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "effective_session_out_of_range"


def test_explicit_in_range_missing_session_is_unavailable(db: Database) -> None:
    days = [D0, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        seed_issuer(
            conn,
            ticker="BBB",
            cik="0001000002",
            facts=annual_roa_facts(
                cik="0001000002",
                fiscal_year=2023,
                net_income=10.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="bbb-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "BBB", [(day, 100.0) for day in days])
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D1,), top_n=2),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "calendar_missing"


def test_unsorted_explicit_dates_are_deterministic(db: Database) -> None:
    _seed_two_name_path(db)
    forward = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1), top_n=2),
    )
    reverse = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D1, D0), top_n=2),
    )
    assert forward.status is PortfolioRunStatus.SUCCESS
    assert reverse.status is PortfolioRunStatus.SUCCESS
    assert [row.decision_at for row in forward.rebalances] == [
        row.decision_at for row in reverse.rebalances
    ]
    assert forward.final_nav == reverse.final_nav
    assert len(forward.rebalances) == 2


def test_duplicate_explicit_dates_collapse_first_seen(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D0, D1), top_n=2),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    assert result.request.explicit_dates == (D0, D0, D1)
    assert len(result.rebalances) == 2
    assert [row.decision_at.date() for row in result.rebalances] == [D0, D1]


def test_explicit_dates_are_not_silently_omitted(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1), top_n=2),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    unique = tuple(dict.fromkeys((D0, D1)))
    assert len(result.rebalances) == len(unique)


def test_explicit_date_not_silently_moved(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(
            ("AAA", "BBB"),
            start=D0,
            end=D3,
            decisions=(date(2024, 1, 1),),
            top_n=2,
        ),
    )
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == "explicit_date_out_of_range"


def test_benchmark_gap_does_not_mutate_weights(db: Database) -> None:
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "QQQ", [(D0, 100.0), (D1, 100.0)])
    result = run_backtest(
        db,
        explicit_request(
            ("AAA",),
            start=D0,
            end=D3,
            decisions=(D0,),
            top_n=1,
            benchmark="QQQ",
        ),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    assert "benchmark_unavailable" in result.warnings
    assert any(point.benchmark_nav is None for point in result.equity)
