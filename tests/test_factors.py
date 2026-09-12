"""Fundamental factor and ranking tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from equitytrace.database import Database
from equitytrace.factors.engine import FactorEngine
from equitytrace.factors.ranking import rank_factors
from equitytrace.financials.periods import parse_period
from equitytrace.financials.service import FinancialsService
from equitytrace.repositories.factors import FactorRepository
from helpers.financial_fixtures import (
    alpha_facts,
    beta_facts,
    dt,
    gamma_facts_missing_revenue,
    seed_company,
)


def _seed_alpha_beta_gamma(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        seed_company(
            conn,
            ticker="BETA",
            cik="0001000002",
            legal_name="Beta Inc",
            facts=beta_facts(),
        )
        seed_company(
            conn,
            ticker="GAMMA",
            cik="0001000003",
            legal_name="Gamma LLC",
            facts=gamma_facts_missing_revenue(),
        )


def test_annual_revenue_growth(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "revenue-growth",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
    assert result.valid
    assert result.value == pytest.approx((120 / 100) - 1)


def test_quarterly_revenue_growth(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "revenue_growth",
            "Q2-2023",
            as_of=dt(2023, 6, 1),
            persist=False,
        )
    # Q2 2023 standalone 30 / Q2 2022 standalone 20 - 1 = 0.5
    assert result.valid
    assert result.value == pytest.approx(0.5)


def test_mismatched_period_rejection_via_growth(db: Database) -> None:
    """Growth requires comparable periods; missing prior quarter fails cleanly."""
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "revenue_growth",
            "Q1-2023",
            as_of=dt(2023, 3, 1),
            persist=False,
        )
    # No prior-year Q1 revenue in fixtures.
    assert result.valid is False
    assert result.unavailable_reason is not None


def test_operating_margin_and_change(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        service = FinancialsService(conn)
        snap = service.get_snapshot("ALPHA", "FY2023", as_of=dt(2023, 12, 1))
        assert snap.require_number("operating_income") == 30.0
        assert snap.require_number("revenue") == 120.0
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "operating-margin-change",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
    # current 30/120=0.25, prior 20/100=0.20, change=0.05
    assert result.valid
    assert result.value == pytest.approx(0.05)
    assert result.inputs["operating_margin"] == pytest.approx(0.25)


def test_free_cash_flow_and_yield_without_market_cap(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        engine = FactorEngine(conn)
        fcf = engine.calculate(
            "ALPHA",
            "free_cash_flow",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
        yield_result = engine.calculate(
            "ALPHA",
            "fcf-yield",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
        with_cap = engine.calculate(
            "ALPHA",
            "fcf_yield",
            "FY2023",
            as_of=dt(2023, 12, 1),
            market_cap=300.0,
            persist=False,
        )
    assert fcf.valid and fcf.value == pytest.approx(30.0)
    assert yield_result.valid is False
    assert "Market capitalization" in (yield_result.unavailable_reason or "")
    assert "instrument_not_found" in (yield_result.unavailable_reason or "")
    assert with_cap.valid and with_cap.value == pytest.approx(0.1)
    assert with_cap.market_input is not None
    assert "manual_market_cap_override" in with_cap.market_input.warnings
    assert with_cap.market_input.price_provider is None


def test_debt_change(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "debt-change",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
    # 40 -> 40? FY2022 debt 50, FY2023 debt 40, change -10
    assert result.valid
    assert result.value == pytest.approx(-10.0)
    assert result.ranking_direction == "lower_is_better"
    assert result.inputs["debt_change_pct"] == pytest.approx(-0.2)


def test_roa_average_assets_and_fallback(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "roa",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
        # Remove beginning assets scenario via BETA without forcing fallback —
        # construct a one-period-only calculation using GAMMA-like path on BETA FY2022
        # where prior FY2021 assets are missing.
        fallback = FactorEngine(conn).calculate(
            "BETA",
            "roa",
            "FY2022",
            as_of=dt(2023, 3, 1),
            persist=False,
        )
    # ALPHA: NI 22 / avg(240,200)=220 => 0.1
    assert result.valid
    assert result.value == pytest.approx(22 / 220)
    assert "roa_fallback_ending_assets" not in result.warnings
    assert fallback.valid
    assert "roa_fallback_ending_assets" in fallback.warnings


def test_accrual_ratio(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "accrual_ratio",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
    # (22 - 40) / 220 = -18/220
    assert result.valid
    assert result.value == pytest.approx((22 - 40) / 220)
    assert result.ranking_direction == "lower_is_better"


def test_missing_input_behavior(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "GAMMA",
            "revenue_growth",
            "FY2023",
            as_of=dt(2024, 4, 1),
            persist=False,
        )
    assert result.valid is False
    assert "missing" in (result.unavailable_reason or "").lower() or result.warnings


def test_factor_calculation_idempotent(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    as_of = dt(2023, 12, 1)
    with db.session() as conn:
        engine = FactorEngine(conn)
        first = engine.calculate("ALPHA", "revenue_growth", "FY2023", as_of=as_of)
        second = engine.calculate("ALPHA", "revenue_growth", "FY2023", as_of=as_of)
        stored = FactorRepository(conn).get_value(
            ticker="ALPHA",
            factor="revenue_growth",
            period=parse_period("FY2023"),
            as_of=as_of,
        )
    assert first.value == second.value
    assert stored == pytest.approx(first.value or 0)


def test_ranking_direction_and_exclusions(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        ranking = rank_factors(
            tickers=["ALPHA", "BETA", "GAMMA"],
            factor="revenue-growth",
            as_of=dt(2024, 3, 1),
            period="FY2023",
            service=FinancialsService(conn),
        )
        debt_ranking = rank_factors(
            tickers=["ALPHA"],
            factor="debt_change",
            as_of=dt(2023, 12, 1),
            period="FY2023",
            service=FinancialsService(conn),
        )
    assert ranking.valid_count == 2
    assert any(symbol == "GAMMA" for symbol, _ in ranking.excluded)
    # ALPHA growth 20%, BETA growth 10% → ALPHA rank 1
    assert ranking.rows[0].result.ticker == "ALPHA"
    assert ranking.rows[0].rank == 1
    assert ranking.rows[0].percentile == pytest.approx(100.0)
    assert ranking.rows[1].percentile == pytest.approx(0.0)
    assert debt_ranking.ranking_direction == "lower_is_better"


def test_percentile_single_name(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        ranking = rank_factors(
            tickers=["ALPHA"],
            factor="revenue_growth",
            as_of=dt(2023, 12, 1),
            period="FY2023",
            service=FinancialsService(conn),
        )
    assert ranking.valid_count == 1
    assert ranking.rows[0].percentile == 100.0


def test_basic_quality_score(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "basic_quality_score",
            "FY2023",
            as_of=dt(2023, 12, 1),
            persist=False,
        )
    assert result.valid
    assert result.value is not None
    assert 0.0 <= result.value <= 1.0


def test_as_of_defaults_timezone_aware(db: Database) -> None:
    _seed_alpha_beta_gamma(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "revenue_growth",
            "FY2023",
            as_of=datetime(2023, 12, 1),  # naive
            persist=False,
        )
    assert result.as_of.tzinfo == UTC
