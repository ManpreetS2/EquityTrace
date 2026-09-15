"""Offline builders for native portfolio backtest tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from equitytrace.database import Database
from equitytrace.market.availability import bar_available_at
from equitytrace.market.models import DailyPriceBar, MarketDataProviderName, PriceAdjustmentMode
from equitytrace.models import FinancialFact, Security
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.portfolio.models import (
    BacktestRequest,
    BacktestResult,
    PortfolioBaseline,
    PortfolioSchedule,
)
from equitytrace.repositories.market import MarketRepository
from helpers.financial_fixtures import make_fact, seed_company

FETCHED = datetime(2025, 1, 2, tzinfo=UTC)


def weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def add_adjusted_bars(
    conn: object,
    ticker: str,
    points: list[tuple[date, float]],
    *,
    available_at: dict[date, datetime] | None = None,
    skip_dates: set[date] | None = None,
    issuer_cik: str | None = None,
    adjustment_mode: PriceAdjustmentMode = PriceAdjustmentMode.ALL,
) -> str:
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    instrument = repo.get_or_create_instrument(
        ticker,
        security_ticker=ticker,
        issuer_cik=issuer_cik,
    )
    bars: list[DailyPriceBar] = []
    skipped = skip_dates or set()
    for day, close in points:
        if day in skipped:
            continue
        known = (available_at or {}).get(day) or bar_available_at(day, "America/New_York")
        price = Decimal(str(close))
        bars.append(
            DailyPriceBar(
                instrument_id=instrument.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                trading_date=day,
                adjustment_mode=adjustment_mode,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=100,
                available_at=known,
                fetched_at=FETCHED,
            )
        )
    if bars:
        repo.upsert_price_bars(bars)
    return instrument.instrument_id


def annual_roa_facts(
    *,
    cik: str,
    fiscal_year: int,
    net_income: float,
    assets: float,
    prior_assets: float,
    available_at: datetime,
    accession: str,
    form: str = "10-K",
) -> list[FinancialFact]:
    start = date(fiscal_year, 1, 1)
    end = date(fiscal_year, 12, 31)
    prior_end = date(fiscal_year - 1, 12, 31)
    prior_start = date(fiscal_year - 1, 1, 1)
    return [
        make_fact(
            cik=cik,
            concept="NetIncomeLoss",
            value=net_income,
            start=start,
            end=end,
            available_at=available_at,
            accession=accession,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="Assets",
            value=assets,
            start=None,
            end=end,
            available_at=available_at,
            accession=accession,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="Assets",
            value=prior_assets,
            start=None,
            end=prior_end,
            available_at=available_at,
            accession=accession + "-P",
            form=form,
            fiscal_year=fiscal_year - 1,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="NetIncomeLoss",
            value=prior_assets / 10.0,
            start=prior_start,
            end=prior_end,
            available_at=available_at,
            accession=accession + "-P",
            form=form,
            fiscal_year=fiscal_year - 1,
            fiscal_period="FY",
        ),
    ]


def annual_fcf_facts(
    *,
    cik: str,
    fiscal_year: int,
    operating_cash_flow: float,
    capex: float,
    shares: float,
    available_at: datetime,
    accession: str,
    shares_accession: str,
    form: str = "10-K",
) -> list[FinancialFact]:
    start = date(fiscal_year, 1, 1)
    end = date(fiscal_year, 12, 31)
    return [
        make_fact(
            cik=cik,
            concept="NetCashProvidedByUsedInOperatingActivities",
            value=operating_cash_flow,
            start=start,
            end=end,
            available_at=available_at,
            accession=accession,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="PaymentsToAcquirePropertyPlantAndEquipment",
            value=capex,
            start=start,
            end=end,
            available_at=available_at,
            accession=accession,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="CommonStockSharesOutstanding",
            value=shares,
            unit="shares",
            start=None,
            end=end,
            available_at=available_at,
            accession=shares_accession,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period="FY",
        ),
    ]


def seed_issuer(
    conn: object,
    *,
    ticker: str,
    cik: str,
    facts: list[FinancialFact],
) -> None:
    seed_company(conn, ticker=ticker, cik=cik, legal_name=f"{ticker} Corp", facts=facts)


def add_share_class(
    conn: object,
    *,
    ticker: str,
    cik: str,
    is_primary: bool = False,
) -> None:
    from equitytrace.repositories.securities import SecuritiesRepository

    SecuritiesRepository(conn).upsert_many(  # type: ignore[arg-type]
        [Security(ticker=ticker, cik=cik, is_primary=is_primary)]
    )


def run_backtest(db: Database, request: BacktestRequest, *, persist: bool = True) -> BacktestResult:
    with db.session() as conn:
        return BacktestEngine(conn).run(request, persist=persist)


def explicit_request(
    tickers: tuple[str, ...],
    *,
    start: date,
    end: date,
    decisions: tuple[date, ...],
    top_n: int = 2,
    baseline: PortfolioBaseline = PortfolioBaseline.EQUAL_WEIGHT,
    factor: str = "roa",
    cost_bps: float = 10.0,
    benchmark: str | None = "SPY",
) -> BacktestRequest:
    return BacktestRequest(
        tickers=tickers,
        factor=factor,
        top_n=top_n,
        baseline=baseline,
        start_date=start,
        end_date=end,
        schedule=PortfolioSchedule.EXPLICIT,
        explicit_dates=decisions,
        cost_bps=cost_bps,
        benchmark_symbol=benchmark,
    )
