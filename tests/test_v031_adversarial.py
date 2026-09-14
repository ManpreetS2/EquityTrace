"""Adversarial correctness tests for EquityTrace v0.3b valuation and analytics."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import Database, initialize_database
from equitytrace.factors.engine import FactorEngine
from equitytrace.market.analytics import (
    LOOKBACK_CALENDAR_DAYS,
    REQUIRED_CLOSES,
    REQUIRED_RETURNS,
    MarketAnalyticsService,
)
from equitytrace.market.availability import bar_available_at
from equitytrace.market.market_cap import DEFAULT_MAX_PRICE_STALENESS_DAYS, MarketCapService
from equitytrace.market.models import (
    DailyPriceBar,
    MarketCapResult,
    MarketDataProviderName,
    PriceAdjustmentMode,
)
from equitytrace.market.shares import SharesOutstandingService
from equitytrace.models import FinancialFact, compute_fact_id
from equitytrace.repositories.factors import FactorRepository
from equitytrace.repositories.market import MarketRepository
from helpers.financial_fixtures import alpha_facts, dt, make_fact, seed_company

AS_OF = datetime(2023, 12, 1, 23, 59, 59, tzinfo=UTC)
PRICE_DAY = date(2023, 12, 1)
ANALYTICS_AS_OF = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)
FETCHED = datetime(2025, 1, 2, tzinfo=UTC)
runner = CliRunner()


def _insert_facts(conn: object, facts: list[FinancialFact]) -> None:
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
    concept: str = "CommonStockSharesOutstanding",
    taxonomy: str = "us-gaap",
) -> None:
    known = available or dt(2023, 11, 2)
    _insert_facts(
        conn,
        [
            FinancialFact(
                cik=cik,
                taxonomy=taxonomy,
                concept=concept,
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
    raw_close: Decimal | None = Decimal("30"),
    adjusted_close: Decimal | None = Decimal("60"),
    currency: str = "USD",
    available_at: datetime | None = None,
) -> None:
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    instrument = repo.get_or_create_instrument(ticker, issuer_cik=cik, security_ticker=ticker)
    fetched = datetime(2024, 1, 1, tzinfo=UTC)
    known = available_at or bar_available_at(trading_date, "America/New_York")
    bars: list[DailyPriceBar] = []
    if raw_close is not None:
        bars.append(
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
            )
        )
    if adjusted_close is not None:
        bars.append(
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
            )
        )
    if bars:
        repo.upsert_price_bars(bars)


def _seed_alpha(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )


def _weekdays_ending_on(end: date, count: int) -> list[date]:
    days: list[date] = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return list(reversed(days))


def _bar(
    instrument_id: str,
    trading_date: date,
    close: Decimal,
    *,
    mode: PriceAdjustmentMode = PriceAdjustmentMode.ALL,
    available_at: datetime | None = None,
) -> DailyPriceBar:
    known = available_at or bar_available_at(trading_date, "America/New_York")
    return DailyPriceBar(
        instrument_id=instrument_id,
        provider=MarketDataProviderName.TWELVE_DATA,
        trading_date=trading_date,
        adjustment_mode=mode,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        available_at=known,
        fetched_at=FETCHED,
    )


def _seed_symbol(
    conn: object,
    ticker: str,
    closes: list[tuple[date, Decimal]],
    *,
    also_raw: bool = True,
) -> str:
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    instrument = repo.get_or_create_instrument(ticker)
    bars = [_bar(instrument.instrument_id, day, close) for day, close in closes]
    if also_raw:
        bars.extend(
            [
                _bar(instrument.instrument_id, day, close * 2, mode=PriceAdjustmentMode.NONE)
                for day, close in closes
            ]
        )
    repo.upsert_price_bars(bars)
    return instrument.instrument_id


def _seed_loss_co(conn: object) -> None:
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


# --- Error precedence -------------------------------------------------------


def test_missing_fcf_not_hidden_by_missing_market_cap(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="NOFCF",
            cik="0001999101",
            legal_name="No FCF Co",
            facts=[
                make_fact(
                    cik="0001999101",
                    concept="NetIncomeLoss",
                    value=4.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="nf-ni",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        result = FactorEngine(conn).calculate(
            "NOFCF", "fcf_yield", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert result.value is None
    assert "free cash flow" in (result.unavailable_reason or "").lower()
    assert "instrument_not_found" not in (result.unavailable_reason or "")
    assert "market_cap_missing" not in result.warnings


def test_negative_earnings_not_hidden_by_missing_market_cap(db: Database) -> None:
    with db.session() as conn:
        _seed_loss_co(conn)
        result = FactorEngine(conn).calculate(
            "LOSS", "price_to_earnings", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert "non_positive_earnings" in result.warnings
    assert "instrument_not_found" not in (result.unavailable_reason or "")
    assert "market_cap_missing" not in result.warnings


def test_zero_revenue_not_hidden_by_missing_market_cap(db: Database) -> None:
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
            "ZREV", "price_to_sales", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert "non_positive_revenue" in result.warnings
    assert "market_cap_missing" not in result.warnings


def test_missing_equity_not_hidden_by_missing_market_cap(db: Database) -> None:
    with db.session() as conn:
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
        result = FactorEngine(conn).calculate(
            "MEQ", "price_to_book", "FY2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert "equity" in (result.unavailable_reason or "").lower()
    assert "market_cap_missing" not in result.warnings


def test_quarterly_pe_with_currency_mismatch_keeps_annual_reason(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn, currency="EUR")
        result = FactorEngine(conn).calculate(
            "ALPHA", "price_to_earnings", "Q2-2023", as_of=AS_OF, persist=False
        )
    assert result.valid is False
    assert "annual_period_required" in result.warnings
    assert "currency" not in (result.unavailable_reason or "").lower()


# --- Non-finite public inputs -----------------------------------------------


@pytest.mark.parametrize("cap", [math.nan, math.inf, -math.inf])
def test_manual_non_finite_market_cap_is_unavailable(db: Database, cap: float) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA",
            "fcf_yield",
            "FY2023",
            as_of=AS_OF,
            market_cap=cap,
            persist=True,
        )
        stored = conn.execute(
            """
            SELECT value, valid FROM factor_values
            WHERE ticker = 'ALPHA' AND factor_name = 'fcf_yield'
            """
        ).fetchone()
    assert result.valid is False
    assert result.value is None
    assert "invalid_market_cap" in result.warnings
    assert result.market_input is not None
    assert "manual_market_cap_override" in result.market_input.warnings
    assert stored is not None
    assert stored[1] is False
    assert stored[0] is None


def test_manual_non_finite_market_cap_does_not_rank(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        ranking = FactorEngine(conn).rank(
            ["ALPHA"],
            "price_to_earnings",
            "FY2023",
            as_of=AS_OF,
            market_cap_by_ticker={"ALPHA": math.nan},
        )
    assert ranking.valid_count == 0
    assert ranking.rows == ()
    assert ranking.excluded[0][0] == "ALPHA"


# --- PIT boundaries / no look-ahead -----------------------------------------


def test_market_cap_available_at_microsecond_boundary(db: Database) -> None:
    _seed_alpha(db)
    available = bar_available_at(PRICE_DAY, "America/New_York")
    with db.session() as conn:
        _add_shares(conn, available=datetime(2023, 11, 2, tzinfo=UTC))
        _add_price(conn, available_at=available)
        svc = MarketCapService(conn)
        before = svc.get_market_cap("ALPHA", PRICE_DAY, as_of=available - timedelta(microseconds=1))
        on = svc.get_market_cap("ALPHA", PRICE_DAY, as_of=available)
        after = svc.get_market_cap("ALPHA", PRICE_DAY, as_of=available + timedelta(microseconds=1))
    assert before.is_available is False
    assert on.is_available
    assert after.is_available
    assert on.market_cap == Decimal("300")


def test_naive_as_of_is_interpreted_as_utc(db: Database) -> None:
    _seed_alpha(db)
    available = bar_available_at(PRICE_DAY, "America/New_York")
    naive = available.replace(tzinfo=None)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn, available_at=available)
        result = FactorEngine(conn).calculate(
            "ALPHA", "fcf_yield", "FY2023", as_of=naive, persist=False
        )
    assert result.valid
    assert result.as_of.tzinfo is not None
    assert result.value == pytest.approx(0.1)


def test_non_utc_as_of_converted_before_cap(db: Database) -> None:
    _seed_alpha(db)
    eastern = datetime(2023, 12, 1, 16, 15, tzinfo=ZoneInfo("America/New_York"))
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn)
        result = FactorEngine(conn).calculate(
            "ALPHA", "price_to_book", "FY2023", as_of=eastern, persist=False
        )
    assert result.valid
    assert result.as_of == eastern.astimezone(UTC)


def test_future_shares_do_not_change_result_at_t(db: Database) -> None:
    _seed_alpha(db)
    later_known = AS_OF + timedelta(minutes=30)
    later_as_of = AS_OF + timedelta(hours=2)
    with db.session() as conn:
        _add_shares(conn, value=10.0, available=datetime(2023, 11, 2, tzinfo=UTC))
        _add_price(conn)
        engine = FactorEngine(conn)
        before = engine.calculate("ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, persist=False)
        _add_shares(
            conn,
            value=20.0,
            available=later_known,
            accession="future-shares",
            end=date(2023, 11, 15),
        )
        still = engine.calculate("ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, persist=False)
        later = engine.calculate(
            "ALPHA",
            "fcf_yield",
            "FY2023",
            as_of=later_as_of,
            persist=False,
        )
    assert before.valid and still.valid
    assert before.value == still.value == pytest.approx(0.1)
    assert later.valid
    assert later.value == pytest.approx(30.0 / (30.0 * 20.0))


def test_future_price_does_not_change_result_at_t(db: Database) -> None:
    _seed_alpha(db)
    later_day = date(2023, 12, 4)
    later_as_of = datetime(2023, 12, 4, 23, 59, 59, tzinfo=UTC)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn, raw_close=Decimal("30"), adjusted_close=Decimal("60"))
        engine = FactorEngine(conn)
        before = engine.calculate(
            "ALPHA", "price_to_earnings", "FY2023", as_of=AS_OF, persist=False
        )
        _add_price(
            conn,
            trading_date=later_day,
            raw_close=Decimal("90"),
            adjusted_close=Decimal("180"),
            available_at=bar_available_at(later_day, "America/New_York"),
        )
        still = engine.calculate("ALPHA", "price_to_earnings", "FY2023", as_of=AS_OF, persist=False)
        later = engine.calculate(
            "ALPHA",
            "price_to_earnings",
            "FY2023",
            as_of=later_as_of,
            persist=False,
        )
    assert before.valid and still.valid
    assert before.value == still.value == pytest.approx(300.0 / 22.0)
    assert later.valid
    assert later.value == pytest.approx(900.0 / 22.0)


def test_future_net_income_restatement_does_not_leak(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="REST",
            cik="0001888001",
            legal_name="Restate Co",
            facts=[
                make_fact(
                    cik="0001888001",
                    concept="NetIncomeLoss",
                    value=10.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2023, 11, 4),
                    accession="orig-ni",
                    form="10-K",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        _add_shares(conn, ticker="REST", cik="0001888001")
        _add_price(conn, ticker="REST", cik="0001888001")
        engine = FactorEngine(conn)
        original = engine.calculate(
            "REST",
            "price_to_earnings",
            "FY2023",
            as_of=AS_OF,
            market_cap=300.0,
            persist=False,
        )
        _insert_facts(
            conn,
            [
                make_fact(
                    cik="0001888001",
                    concept="NetIncomeLoss",
                    value=20.0,
                    start=date(2022, 10, 1),
                    end=date(2023, 9, 30),
                    available_at=dt(2024, 2, 1),
                    accession="rest-ni",
                    form="10-K/A",
                    fiscal_year=2023,
                    fiscal_period="FY",
                )
            ],
        )
        still = engine.calculate(
            "REST",
            "price_to_earnings",
            "FY2023",
            as_of=AS_OF,
            market_cap=300.0,
            persist=False,
        )
        after = engine.calculate(
            "REST",
            "price_to_earnings",
            "FY2023",
            as_of=datetime(2024, 3, 1, tzinfo=UTC),
            market_cap=300.0,
            persist=False,
        )
    assert original.valid and still.valid
    assert original.value == still.value == pytest.approx(300.0 / 10.0)
    assert after.valid
    assert after.value == pytest.approx(300.0 / 20.0)
    assert "rest-ni" in after.source_filings


def test_analytics_future_bar_does_not_change_momentum_at_t(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[0] = Decimal("50")
    future_day = ANALYTICS_AS_OF.date() + timedelta(days=2)
    while future_day.weekday() >= 5:
        future_day += timedelta(days=1)
    with db.session() as conn:
        instrument_id = _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        svc = MarketAnalyticsService(conn)
        before = svc.calculate("AAA", "momentum_12_1", ANALYTICS_AS_OF)
        MarketRepository(conn).upsert_price_bars([_bar(instrument_id, future_day, Decimal("1"))])
        still = svc.calculate("AAA", "momentum_12_1", ANALYTICS_AS_OF)
    assert before.valid and still.valid
    assert before.value == still.value == pytest.approx(1.0)
    assert still.last_trading_date != future_day


# --- Market cap -------------------------------------------------------------


def test_market_cap_ignores_adjusted_when_raw_missing(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn, raw_close=None, adjusted_close=Decimal("60"))
        result = MarketCapService(conn).get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
    assert result.is_available is False
    assert result.unavailable_reason == "missing_raw_price"


def test_market_cap_uses_raw_when_adjusted_missing(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn, raw_close=Decimal("30"), adjusted_close=None)
        result = MarketCapService(conn).get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
    assert result.is_available
    assert result.market_cap == Decimal("300")
    assert result.price_adjustment_mode == PriceAdjustmentMode.NONE


def test_zero_and_negative_shares_unavailable(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_price(conn)
        _add_shares(conn, value=0.0, accession="zero-sh")
        zero = MarketCapService(conn).get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
        _add_shares(conn, value=-5.0, accession="neg-sh")
        neg = MarketCapService(conn).get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
    assert zero.is_available is False
    assert neg.is_available is False


def test_shares_ending_after_price_date_ignored(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_price(conn)
        _add_shares(conn, value=10.0, end=date(2023, 11, 1), accession="old-sh")
        _add_shares(conn, value=99.0, end=date(2023, 12, 15), accession="future-end")
        result = MarketCapService(conn).get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
    assert result.is_available
    assert result.shares_outstanding == Decimal("10")


def test_stale_price_threshold_boundary(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        exact = PRICE_DAY - timedelta(days=DEFAULT_MAX_PRICE_STALENESS_DAYS)
        over = PRICE_DAY - timedelta(days=DEFAULT_MAX_PRICE_STALENESS_DAYS + 1)
        _add_price(conn, trading_date=exact, raw_close=Decimal("30"))
        svc = MarketCapService(conn)
        ok = svc.get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
        _add_price(conn, ticker="BETA", cik="0001000001", trading_date=over)
        # BETA is same issuer unless we seed separately; use ALPHA with only over date
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("ALPHA")
        assert instrument is not None
        repo.upsert_price_bars(
            [
                _bar(
                    instrument.instrument_id,
                    over,
                    Decimal("30"),
                    mode=PriceAdjustmentMode.NONE,
                )
            ]
        )
        stale = svc.get_market_cap(
            "ALPHA",
            PRICE_DAY + timedelta(days=30),
            as_of=datetime(2024, 1, 15, tzinfo=UTC),
            max_price_staleness_days=DEFAULT_MAX_PRICE_STALENESS_DAYS,
        )
    assert ok.is_available
    assert stale.is_available is False
    assert "price_stale" in (stale.unavailable_reason or "")


def test_lowercase_ticker_market_cap(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn)
        result = MarketCapService(conn).get_market_cap("alpha", PRICE_DAY, as_of=AS_OF)
    assert result.ticker == "ALPHA"
    assert result.market_cap == Decimal("300")


def test_conflicting_equal_priority_shares_warn_and_remain_deterministic(db: Database) -> None:
    """Ambiguous equal-priority shares keep a deterministic pick plus a warning.

    This matches financial-statement conflict handling rather than fabricating a
    random value. The warning must be present so callers can treat it as unsafe.
    """
    _seed_alpha(db)
    known = dt(2023, 11, 2)
    with db.session() as conn:
        _add_price(conn)
        _add_shares(conn, value=10.0, accession="sh-a", available=known)
        _add_shares(conn, value=12.0, accession="sh-b", available=known)
        shares = SharesOutstandingService(conn).get_shares_outstanding(
            "ALPHA", market_date=PRICE_DAY, knowledge_time=AS_OF
        )
        first = MarketCapService(conn).get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
        second = MarketCapService(conn).get_market_cap("ALPHA", PRICE_DAY, as_of=AS_OF)
    assert shares.is_available
    assert "conflicting_equal_priority_shares_facts" in shares.warnings
    assert first.market_cap == second.market_cap
    assert first.sec_accession == second.sec_accession


# --- Ranking ----------------------------------------------------------------


def test_factor_rank_deduplicates_mixed_case_and_repeats(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        _add_shares(conn)
        _add_price(conn)
        ranking = FactorEngine(conn).rank(
            ["alpha", "ALPHA", " ALPHA ", ""],
            "price_to_book",
            "FY2023",
            as_of=AS_OF,
        )
    assert ranking.valid_count == 1
    assert [row.result.ticker for row in ranking.rows] == ["ALPHA"]


def test_factor_rank_manual_cap_keys_are_case_insensitive(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        ranking = FactorEngine(conn).rank(
            ["alpha"],
            "price_to_sales",
            "FY2023",
            as_of=AS_OF,
            market_cap_by_ticker={"alpha": 240.0},
        )
    assert ranking.valid_count == 1
    assert ranking.rows[0].result.value == pytest.approx(2.0)


def test_market_rank_deduplicates_input_tickers(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10")) for d in dates])
        ranking = MarketAnalyticsService(conn).rank(
            ["aaa", "AAA", ""],
            "max_drawdown_1y",
            ANALYTICS_AS_OF,
        )
    assert ranking.valid_count == 1
    assert [row.result.ticker for row in ranking.rows] == ["AAA"]


def test_three_way_tie_competition_rank_is_deterministic(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        for ticker in ("CCC", "AAA", "BBB"):
            _seed_symbol(conn, ticker, [(d, Decimal("10")) for d in dates])
        svc = MarketAnalyticsService(conn)
        forward = svc.rank(["CCC", "BBB", "AAA"], "momentum_12_1", ANALYTICS_AS_OF)
        reverse = svc.rank(["AAA", "BBB", "CCC"], "momentum_12_1", ANALYTICS_AS_OF)
    tickers = [row.result.ticker for row in forward.rows]
    assert tickers == ["AAA", "BBB", "CCC"]
    assert tickers == [row.result.ticker for row in reverse.rows]
    assert [row.rank for row in forward.rows] == [1, 1, 1]


# --- Analytics edges --------------------------------------------------------


def test_momentum_rejects_non_positive_end_close(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[-22] = Decimal("0")
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        result = MarketAnalyticsService(conn).calculate("AAA", "momentum_12_1", ANALYTICS_AS_OF)
    assert result.valid is False
    assert result.value is None
    assert result.unavailable_reason is not None


def test_momentum_rejects_negative_end_close(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[-22] = Decimal("-5")
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        result = MarketAnalyticsService(conn).calculate("AAA", "momentum_12_1", ANALYTICS_AS_OF)
    assert result.valid is False
    assert result.value is None


def test_volatility_constant_prices_are_zero_not_nan(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("42")) for d in dates])
        result = MarketAnalyticsService(conn).calculate("AAA", "volatility_1y", ANALYTICS_AS_OF)
    assert result.valid
    assert result.value is not None
    assert math.isfinite(result.value)
    assert result.value == pytest.approx(0.0)


def test_drawdown_flat_and_rising_series_are_zero(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    rising = [Decimal(str(10 + i)) for i in range(REQUIRED_CLOSES)]
    with db.session() as conn:
        _seed_symbol(conn, "FLT", [(d, Decimal("10")) for d in dates])
        _seed_symbol(conn, "UP", list(zip(dates, rising, strict=True)))
        svc = MarketAnalyticsService(conn)
        flat = svc.calculate("FLT", "max_drawdown_1y", ANALYTICS_AS_OF)
        up = svc.calculate("UP", "max_drawdown_1y", ANALYTICS_AS_OF)
    assert flat.valid and up.valid
    assert flat.value is not None and up.value is not None
    assert flat.value == pytest.approx(0.0)
    assert up.value == pytest.approx(0.0)
    assert flat.value <= 0
    assert up.value <= 0


def test_beta_identity_and_inverse(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    spy = [Decimal(str(100 + (i % 7))) for i in range(REQUIRED_CLOSES)]
    inverse = [Decimal("10000") / px for px in spy]
    with db.session() as conn:
        _seed_symbol(conn, "SPY", list(zip(dates, spy, strict=True)))
        _seed_symbol(conn, "AAA", list(zip(dates, spy, strict=True)))
        _seed_symbol(conn, "INV", list(zip(dates, inverse, strict=True)))
        svc = MarketAnalyticsService(conn)
        identity = svc.calculate("AAA", "beta_1y", ANALYTICS_AS_OF)
        vs_self = svc.calculate("spy", "beta_1y", ANALYTICS_AS_OF, benchmark="spy")
        inv = svc.calculate("INV", "beta_1y", ANALYTICS_AS_OF)
    assert identity.valid and vs_self.valid
    assert identity.value == pytest.approx(1.0, rel=1e-9)
    assert vs_self.value == pytest.approx(1.0, rel=1e-9)
    assert inv.valid
    assert (inv.value or 0) < 0


def test_beta_benchmark_available_only_after_as_of(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    late = ANALYTICS_AS_OF + timedelta(days=1)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10") + Decimal(i)) for i, d in enumerate(dates)])
        repo = MarketRepository(conn)
        spy = repo.get_or_create_instrument("SPY")
        repo.upsert_price_bars(
            [_bar(spy.instrument_id, d, Decimal("20"), available_at=late) for d in dates]
        )
        result = MarketAnalyticsService(conn).calculate("AAA", "beta_1y", ANALYTICS_AS_OF)
    assert result.valid is False
    assert result.unavailable_reason in {
        "insufficient_aligned_observations",
        "insufficient_history",
    }


def test_beta_result_is_finite_for_near_constant_benchmark(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    spy = [Decimal("100") + Decimal(i) * Decimal("1e-12") for i in range(REQUIRED_CLOSES)]
    aaa = [Decimal("10") * Decimal(i + 1) for i in range(REQUIRED_CLOSES)]
    with db.session() as conn:
        _seed_symbol(conn, "SPY", list(zip(dates, spy, strict=True)))
        _seed_symbol(conn, "AAA", list(zip(dates, aaa, strict=True)))
        result = MarketAnalyticsService(conn).calculate("AAA", "beta_1y", ANALYTICS_AS_OF)
    if result.valid:
        assert result.value is not None
        assert math.isfinite(result.value)
    else:
        assert result.unavailable_reason in {
            "zero_benchmark_variance",
            "non_finite_result",
        }


def test_lookback_covers_253_weekdays(db: Database) -> None:
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    span = (dates[-1] - dates[0]).days
    assert span < LOOKBACK_CALENDAR_DAYS
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10")) for d in dates])
        result = MarketAnalyticsService(conn).calculate("AAA", "volatility_1y", ANALYTICS_AS_OF)
    assert result.valid


def test_beta_provenance_uses_common_window(db: Database) -> None:
    asset_dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES + 5)
    spy_dates = asset_dates[2:]
    with db.session() as conn:
        _seed_symbol(
            conn,
            "AAA",
            [(d, Decimal("10") + Decimal(i)) for i, d in enumerate(asset_dates)],
        )
        _seed_symbol(
            conn,
            "SPY",
            [(d, Decimal("20") + Decimal(i)) for i, d in enumerate(spy_dates)],
        )
        result = MarketAnalyticsService(conn).calculate("AAA", "beta_1y", ANALYTICS_AS_OF)
    assert result.valid
    assert result.window_start == spy_dates[-(REQUIRED_RETURNS + 1)]
    assert result.window_end == spy_dates[-1]
    assert result.benchmark == "SPY"


# --- Persistence / provenance -----------------------------------------------


def test_unavailable_valuation_still_persists_market_input(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        FactorEngine(conn).calculate("ALPHA", "price_to_earnings", "FY2023", as_of=AS_OF)
        row = conn.execute(
            """
            SELECT valid, market_input_json, unavailable_reason
            FROM factor_values
            WHERE ticker = 'ALPHA' AND factor_name = 'price_to_earnings'
            """
        ).fetchone()
    assert row is not None
    assert row[0] is False
    assert row[1] is not None
    payload = json.loads(row[1])
    assert payload["unavailable_reason"] == "instrument_not_found"
    assert "instrument_not_found" in row[2]


def test_manual_override_persists_without_provider_fields(db: Database) -> None:
    _seed_alpha(db)
    with db.session() as conn:
        result = FactorEngine(conn).calculate(
            "ALPHA", "fcf_yield", "FY2023", as_of=AS_OF, market_cap=300.0
        )
        row = conn.execute(
            "SELECT market_input_json FROM factor_values WHERE factor_name = 'fcf_yield'"
        ).fetchone()
        again = FactorRepository(conn).get_value(
            ticker="ALPHA",
            factor="fcf_yield",
            period=result.period,
            as_of=result.as_of,
        )
    assert result.valid
    assert row is not None
    payload = json.loads(row[0])
    parsed = MarketCapResult.model_validate(payload)
    assert "manual_market_cap_override" in parsed.warnings
    assert parsed.price_provider is None
    assert parsed.price_fetched_at is None
    assert again == pytest.approx(0.1)


# --- Property tests ---------------------------------------------------------


def test_scale_invariance_and_drawdown_sign_properties(db: Database) -> None:
    rng = random.Random(42)
    dates = _weekdays_ending_on(ANALYTICS_AS_OF.date(), REQUIRED_CLOSES)
    prices = [Decimal("50")]
    for _ in range(REQUIRED_CLOSES - 1):
        move = Decimal(str(1 + rng.uniform(-0.03, 0.03)))
        prices.append(max(Decimal("0.5"), prices[-1] * move))
    scaled = [px * Decimal("7.5") for px in prices]
    spy = [Decimal("100")]
    for _ in range(REQUIRED_CLOSES - 1):
        move = Decimal(str(1 + rng.uniform(-0.02, 0.02)))
        spy.append(max(Decimal("1"), spy[-1] * move))
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, prices, strict=True)))
        _seed_symbol(conn, "BBB", list(zip(dates, scaled, strict=True)))
        _seed_symbol(conn, "SPY", list(zip(dates, spy, strict=True)))
        svc = MarketAnalyticsService(conn)
        mom_a = svc.calculate("AAA", "momentum_12_1", ANALYTICS_AS_OF)
        mom_b = svc.calculate("BBB", "momentum_12_1", ANALYTICS_AS_OF)
        vol_a = svc.calculate("AAA", "volatility_1y", ANALYTICS_AS_OF)
        vol_b = svc.calculate("BBB", "volatility_1y", ANALYTICS_AS_OF)
        dd_a = svc.calculate("AAA", "max_drawdown_1y", ANALYTICS_AS_OF)
        dd_b = svc.calculate("BBB", "max_drawdown_1y", ANALYTICS_AS_OF)
        beta_a = svc.calculate("AAA", "beta_1y", ANALYTICS_AS_OF)
        beta_b = svc.calculate("BBB", "beta_1y", ANALYTICS_AS_OF)
    assert mom_a.valid and mom_b.valid
    assert mom_a.value == pytest.approx(mom_b.value or 0)
    assert vol_a.valid and vol_b.valid
    assert vol_a.value is not None and vol_b.value is not None
    assert vol_a.value >= 0
    assert vol_a.value == pytest.approx(vol_b.value)
    assert dd_a.valid and dd_b.valid
    assert dd_a.value is not None and dd_b.value is not None
    assert dd_a.value <= 0
    assert dd_a.value == pytest.approx(dd_b.value)
    assert beta_a.valid and beta_b.valid
    assert beta_a.value is not None and beta_b.value is not None
    assert beta_a.value == pytest.approx(beta_b.value, rel=1e-6, abs=1e-12)


# --- CLI --------------------------------------------------------------------


def _cli_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "adv.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    clear_settings_cache()
    initialize_database(path)
    return path


def test_cli_invalid_inputs_exit_nonzero_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _cli_db(tmp_path, monkeypatch)
    missing = runner.invoke(
        app,
        [
            "factor",
            "ALPHA",
            "fcf_yield",
            "--period",
            "FY2023",
            "--database",
            str(tmp_path / "no.db"),
        ],
    )
    bad_period = runner.invoke(
        app,
        ["factor", "ALPHA", "fcf_yield", "--period", "YEAR2023", "--database", str(path)],
    )
    bad_date = runner.invoke(
        app,
        [
            "market",
            "analytics",
            "AAA",
            "--as-of",
            "not-a-date",
            "--database",
            str(path),
        ],
    )
    bad_metric = runner.invoke(
        app,
        [
            "market",
            "rank-metric",
            "not_a_metric",
            "AAA",
            "--as-of",
            "2024-12-31",
            "--database",
            str(path),
        ],
    )
    for result in (missing, bad_period, bad_date, bad_metric):
        assert result.exit_code != 0, result.output
        assert "Traceback" not in result.output


def test_cli_unavailable_research_is_inspectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _cli_db(tmp_path, monkeypatch)
    db = initialize_database(path)
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
    result = runner.invoke(
        app,
        [
            "factor",
            "alpha",
            "pe",
            "--period",
            "FY2023",
            "--as-of",
            "2023-12-01",
            "--database",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "no" in result.output.lower()
    analytics = runner.invoke(
        app,
        ["market", "analytics", "zzz", "--as-of", "2024-12-31", "--database", str(path)],
    )
    assert analytics.exit_code == 0, analytics.output
    assert "Traceback" not in analytics.output
