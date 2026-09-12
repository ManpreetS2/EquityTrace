"""Native valuation-factor tests using stored point-in-time market cap."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from equitytrace.database import Database
from equitytrace.factors.engine import FactorEngine
from equitytrace.market.availability import bar_available_at
from equitytrace.market.models import (
    DailyPriceBar,
    MarketDataProviderName,
    PriceAdjustmentMode,
)
from equitytrace.models import FinancialFact, Security, compute_fact_id
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository
from helpers.financial_fixtures import (
    alpha_facts,
    dt,
    make_fact,
    seed_company,
)

AS_OF = datetime(2023, 12, 1, 23, 59, 59, tzinfo=UTC)
PRICE_DAY = date(2023, 12, 1)


def _seed_alpha(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )


def _insert_facts(conn: object, facts: list[FinancialFact]) -> None:
    # Additive insert. FactsRepository.upsert_many replaces all facts for a CIK.
    now = datetime.now(UTC)
    for fact in facts:
        fact_id = fact.fact_id or compute_fact_id(fact)
        conn.execute(  # type: ignore[union-attr]
            """
            INSERT INTO financial_facts (
                fact_id, cik, taxonomy, concept, label, description, unit, value,
                start_date, end_date, filing_date, acceptance_datetime, available_at,
                accession_number, form, fiscal_year, fiscal_period, frame, source_url,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fact_id) DO UPDATE SET
                value = excluded.value,
                available_at = excluded.available_at,
                updated_at = excluded.updated_at
            """,
            [
                fact_id,
                fact.cik,
                fact.taxonomy,
                fact.concept,
                fact.label,
                fact.description,
                fact.unit,
                fact.value,
                fact.start_date,
                fact.end_date,
                fact.filing_date,
                fact.acceptance_datetime,
                fact.available_at,
                fact.accession_number,
                fact.form,
                fact.fiscal_year,
                fact.fiscal_period,
                fact.frame,
                fact.source_url,
                now,
                now,
            ],
        )


def _add_shares(
    conn: object,
    *,
    ticker: str = "ALPHA",
    cik: str = "0001000001",
    value: float = 10.0,
    end: date = date(2023, 11, 1),
    available: datetime | None = None,
    accession: str = "0001000001-23-000200",
) -> None:
    known = available or dt(2023, 11, 2)
    _insert_facts(
        conn,
        [
            FinancialFact(
                cik=cik,
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstanding",
                unit="shares",
                value=value,
                end_date=end,
                available_at=known,
                accession_number=accession,
                form="10-K",
                filing_date=end,
            )
        ],
    )


def _add_price(
    conn: object,
    *,
    ticker: str = "ALPHA",
    cik: str = "0001000001",
    trading_date: date = PRICE_DAY,
    raw_close: Decimal = Decimal("30"),
    adjusted_close: Decimal = Decimal("60"),
    currency: str = "USD",
    available_at: datetime | None = None,
) -> None:
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    instrument = repo.get_or_create_instrument(ticker, issuer_cik=cik, security_ticker=ticker)
    fetched = datetime(2024, 1, 1, tzinfo=UTC)
    known = available_at or bar_available_at(trading_date, "America/New_York")
    repo.upsert_price_bars(
        [
            DailyPriceBar(
                instrument_id=instrument.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                trading_date=trading_date,
                adjustment_mode=PriceAdjustmentMode.NONE,
                open=raw_close,
                high=raw_close,
                low=raw_close,
                close=raw_close,
                volume=10,
                currency=currency,
                available_at=known,
                fetched_at=fetched,
            ),
            DailyPriceBar(
                instrument_id=instrument.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                trading_date=trading_date,
                adjustment_mode=PriceAdjustmentMode.ALL,
                open=adjusted_close,
                high=adjusted_close,
                low=adjusted_close,
                close=adjusted_close,
                volume=10,
                currency=currency,
                available_at=known,
                fetched_at=fetched,
            ),
        ]
    )


def _native_cap(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn)


def test_fcf_yield_uses_stored_raw_market_cap(db: Database) -> None:
    _native_cap(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, persist=False
        )
    # FCF 30 / (raw 30 * 10 shares). Adjusted close 60 must not be used.
    assert result.valid
    assert result.value == pytest.approx(0.1)
    assert result.market_input is not None
    assert result.market_input.raw_close == Decimal("30")
    assert result.market_input.price_adjustment_mode == PriceAdjustmentMode.NONE
    assert result.market_input.price_provider == MarketDataProviderName.TWELVE_DATA
    assert "0001000001-23-000100" in result.source_filings


def test_fcf_yield_honors_as_of(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn, available=datetime(2023, 12, 15, tzinfo=UTC))
        _add_price(conn)
        _add_price(conn, trading_date=date(2023, 12, 20))
        early = FactorEngine(conn).calculate(
            "ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, persist=False
        )
        later = FactorEngine(conn).calculate(
            "ALPHA",
            "fcf_yield",
            "FY2023",
            as_of=datetime(2023, 12, 20, 23, 59, 59, tzinfo=UTC),
            persist=False,
        )
    assert early.valid is False
    assert later.valid and later.value == pytest.approx(0.1)


def test_fcf_yield_missing_shares_unavailable(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_price(conn)
        result = FactorEngine(conn).calculate(
            "ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert result.market_input is not None
    assert result.market_input.unavailable_reason is not None


def test_fcf_yield_stale_price_unavailable(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn, trading_date=date(2023, 11, 1))
        result = FactorEngine(conn).calculate(
            "ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert "price_stale" in (result.unavailable_reason or "")


def test_fcf_yield_multi_class_unavailable(db: Database) -> None:
    _native_cap(db)
    with db.session() as conn:
        SecuritiesRepository(conn).upsert_many(
            [Security(ticker="ALPHB", cik="0001000001", is_primary=False)]
        )
        result = FactorEngine(conn).calculate(
            "ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert "multi_class" in (result.unavailable_reason or "")


def test_fcf_yield_explicit_override_is_not_native(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "fcf_yield",
            "FY2023",
            as_of=AS_OF,
            market_cap=300.0,
            persist=False,
        )
    assert result.valid and result.value == pytest.approx(0.1)
    assert result.market_input is not None
    assert "manual_market_cap_override" in result.market_input.warnings
    assert result.market_input.price_provider is None


def test_pe_fy_formula_and_quarterly_unavailable(db: Database) -> None:
    _native_cap(db)
    with db.session() as conn:
        engine = FactorEngine(conn)
        fy = engine.calculate("ALPHA", "pe", "FY2023", as_of=AS_OF, persist=False)
        q = engine.calculate("ALPHA", "p-e", "Q1-2023", as_of=AS_OF, persist=False)
    assert fy.valid
    assert fy.value == pytest.approx(300.0 / 22.0)
    assert fy.ranking_direction == "lower_is_better"
    assert "0001000001-23-000100" in fy.source_filings
    assert q.valid is False
    assert "annual_period_required" in q.warnings


def test_pe_missing_zero_negative_earnings(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ZERO",
            cik="0001999001",
            legal_name="Zero Earn",
            facts=[
                make_fact(
                    cik="0001999001",
                    concept="NetIncomeLoss",
                    value=0.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="z-ni",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        seed_company(
            conn,
            ticker="LOSS",
            cik="0001999002",
            legal_name="Loss Co",
            facts=[
                make_fact(
                    cik="0001999002",
                    concept="NetIncomeLoss",
                    value=-5.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="l-ni",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        seed_company(
            conn,
            ticker="MISS",
            cik="0001999003",
            legal_name="Miss Co",
            facts=[
                make_fact(
                    cik="0001999003",
                    concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                    value=10.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="m-rev",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        engine = FactorEngine(conn)
        zero = engine.calculate(
            "ZERO", "price_to_earnings", "FY2023", as_of=AS_OF, market_cap=100.0, persist=False
        )
        loss = engine.calculate(
            "LOSS", "price_to_earnings", "FY2023", as_of=AS_OF, market_cap=100.0, persist=False
        )
        missing = engine.calculate(
            "MISS", "price_to_earnings", "FY2023", as_of=AS_OF, market_cap=100.0, persist=False
        )
    assert zero.valid is False and loss.valid is False and missing.valid is False
    assert zero.value is None and loss.value is None


def test_pe_currency_mismatch_unavailable(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn, currency="EUR")
        result = FactorEngine(conn).calculate(
            "ALPHA", "price_to_earnings", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert "currency" in (result.unavailable_reason or "").lower()
    assert result.market_input is not None
    assert result.market_input.currency == "EUR"


def test_ps_fy_and_quarterly(db: Database) -> None:
    _native_cap(db)
    with db.session() as conn:
        engine = FactorEngine(conn)
        fy = engine.calculate("ALPHA", "ps", "FY2023", as_of=AS_OF, persist=False)
        q = engine.calculate("ALPHA", "price-to-sales", "Q1-2023", as_of=AS_OF, persist=False)
        missing = engine.calculate(
            "ALPHA",
            "price_to_sales",
            "FY2023",
            as_of=dt(2023, 10, 1),
            market_cap=300.0,
            persist=False,
        )
    assert fy.valid
    assert fy.value == pytest.approx(2.5)
    assert fy.ranking_direction == "lower_is_better"
    assert q.valid is False
    assert "annual_period_required" in q.warnings
    assert missing.valid is False


def test_ps_zero_revenue_unavailable(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ZREV",
            cik="0001999004",
            legal_name="Zero Rev",
            facts=[
                make_fact(
                    cik="0001999004",
                    concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                    value=0.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="z-rev",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        result = FactorEngine(conn).calculate(
            "ZREV", "price_to_sales", "FY2023", as_of=AS_OF, market_cap=100.0, persist=False
        )
    assert result.valid is False


def test_pb_annual_and_quarterly(db: Database) -> None:
    _native_cap(db)
    with db.session() as conn:
        _insert_facts(
            conn,
            [
                make_fact(
                    cik="0001000001",
                    concept="StockholdersEquity",
                    value=75.0,
                    end=date(2023, 6, 30),
                    available_at=dt(2023, 8, 4),
                    accession="0001000001-23-000033",
                    form="10-Q",
                    fiscal_year=2023,
                    fiscal_period="Q3",
                )
            ],
        )
        engine = FactorEngine(conn)
        fy = engine.calculate("ALPHA", "pb", "FY2023", as_of=AS_OF, persist=False)
        q = engine.calculate("ALPHA", "price_to_book", "Q3-2023", as_of=AS_OF, persist=False)
    assert fy.valid
    assert fy.value == pytest.approx(2.0)
    assert q.valid
    assert q.value == pytest.approx(4.0)
    assert "0001000001-23-000033" in q.source_filings


def test_pb_missing_zero_negative_equity(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ZEQ",
            cik="0001999005",
            legal_name="Zero Eq",
            facts=[
                make_fact(
                    cik="0001999005",
                    concept="StockholdersEquity",
                    value=0.0,
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="z-eq",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        seed_company(
            conn,
            ticker="NEQ",
            cik="0001999006",
            legal_name="Neg Eq",
            facts=[
                make_fact(
                    cik="0001999006",
                    concept="StockholdersEquity",
                    value=-10.0,
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="n-eq",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        seed_company(
            conn,
            ticker="MEQ",
            cik="0001999007",
            legal_name="Miss Eq",
            facts=[
                make_fact(
                    cik="0001999007",
                    concept="NetIncomeLoss",
                    value=1.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="m-eq",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        engine = FactorEngine(conn)
        zero = engine.calculate(
            "ZEQ", "price_to_book", "FY2023", as_of=AS_OF, market_cap=50.0, persist=False
        )
        neg = engine.calculate(
            "NEQ", "price_to_book", "FY2023", as_of=AS_OF, market_cap=50.0, persist=False
        )
        missing = engine.calculate(
            "MEQ", "price_to_book", "FY2023", as_of=AS_OF, market_cap=50.0, persist=False
        )
    assert zero.valid is False and neg.valid is False and missing.valid is False


def test_valuation_ranking_deterministic(db: Database) -> None:
    _native_cap(db)
    with db.session() as conn:
        seed_company(
            conn,
            ticker="BETA",
            cik="0001000002",
            legal_name="Beta Inc",
            facts=[
                make_fact(
                    cik="0001000002",
                    concept="NetIncomeLoss",
                    value=50.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="b-ni",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        _add_shares(conn, ticker="BETA", cik="0001000002", accession="b-sh")
        _add_price(conn, ticker="BETA", cik="0001000002")
        engine = FactorEngine(conn)
        forward = engine.rank(["BETA", "ALPHA"], "price_to_earnings", "FY2023", as_of=AS_OF)
        reverse = engine.rank(["ALPHA", "BETA"], "price_to_earnings", "FY2023", as_of=AS_OF)
    assert [row.result.ticker for row in forward.rows] == [
        row.result.ticker for row in reverse.rows
    ]
    assert forward.rows[0].result.ticker == "BETA"
    assert forward.rows[0].rank == 1
    assert forward.rows[1].result.ticker == "ALPHA"


def test_factor_persistence_keeps_market_input(db: Database) -> None:
    _native_cap(db)
    with db.session() as conn:
        FactorEngine(conn).calculate("ALPHA", "fcf_yield", "FY2023", as_of=AS_OF)
        row = conn.execute(
            """
            SELECT market_input_json, source_filings_json
            FROM factor_values
            WHERE ticker = 'ALPHA' AND factor_name = 'fcf_yield'
            """
        ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert "twelve_data" in row[0]
    assert "0001000001-23-000100" in row[1]
