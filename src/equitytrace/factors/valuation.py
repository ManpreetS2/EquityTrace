"""Valuation multiples using stored point-in-time market capitalization."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from equitytrace.factors.base import invalid_result, snapshot_accessions
from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.service import FinancialsService

_ANNUAL_REQUIRED = "annual_period_required"


def _market_cap_inputs(
    market_cap: float | None, **fundamentals: float | None
) -> dict[str, float | None]:
    return {"market_cap": market_cap, **fundamentals}


def _missing_market_cap(
    *,
    ticker: str,
    cik: str,
    factor: str,
    as_of: datetime,
    period: FinancialPeriod,
    inputs: dict[str, float | None],
    filings: tuple[str, ...],
    ranking_direction: Literal["higher_is_better", "lower_is_better"],
) -> FactorResult:
    return invalid_result(
        ticker=ticker,
        cik=cik,
        factor=factor,
        as_of=as_of,
        period=period,
        reason=(
            "Market capitalization unavailable. Store point-in-time market data or pass market_cap."
        ),
        inputs=inputs,
        source_filings=filings,
        warnings=("market_cap_missing",),
        ranking_direction=ranking_direction,
    )


def _invalid_market_cap(
    *,
    ticker: str,
    cik: str,
    factor: str,
    as_of: datetime,
    period: FinancialPeriod,
    inputs: dict[str, float | None],
    filings: tuple[str, ...],
    ranking_direction: Literal["higher_is_better", "lower_is_better"],
) -> FactorResult:
    return invalid_result(
        ticker=ticker,
        cik=cik,
        factor=factor,
        as_of=as_of,
        period=period,
        reason="Market capitalization must be positive",
        inputs=inputs,
        source_filings=filings,
        warnings=("invalid_market_cap",),
        ranking_direction=ranking_direction,
    )


def _annual_only(
    *,
    ticker: str,
    cik: str,
    factor: str,
    as_of: datetime,
    period: FinancialPeriod,
    inputs: dict[str, float | None],
    filings: tuple[str, ...],
    ranking_direction: Literal["higher_is_better", "lower_is_better"],
) -> FactorResult:
    # Quarterly P/E and P/S would silently mix period lengths without TTM assembly.
    return invalid_result(
        ticker=ticker,
        cik=cik,
        factor=factor,
        as_of=as_of,
        period=period,
        reason="P/E and P/S require an annual FY period (quarterly TTM is not assembled)",
        inputs=inputs,
        source_filings=filings,
        warnings=(_ANNUAL_REQUIRED,),
        ranking_direction=ranking_direction,
    )


class PriceToEarningsFactor:
    """FY P/E = PIT market cap / period net income. Quarterly periods are unavailable."""

    name: str = "price_to_earnings"
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "lower_is_better"
    requires_market_cap: bool = True

    def calculate(
        self,
        ticker: str,
        as_of: datetime,
        period: FinancialPeriod,
        *,
        service: FinancialsService,
        market_cap: float | None = None,
    ) -> FactorResult:
        snap = service.get_snapshot(ticker, period, as_of=as_of)
        net_income = snap.require_number("net_income")
        inputs = _market_cap_inputs(market_cap, net_income=net_income)
        filings = snapshot_accessions(snap, "net_income")
        if period.kind != "annual":
            return _annual_only(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if market_cap is None:
            return _missing_market_cap(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if market_cap <= 0:
            return _invalid_market_cap(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if net_income is None:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Net income unavailable",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if net_income <= 0:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Net income must be positive (negative P/E is not returned)",
                inputs=inputs,
                source_filings=filings,
                warnings=("non_positive_earnings",),
                ranking_direction=self.ranking_direction,
            )
        return FactorResult(
            ticker=snap.ticker,
            cik=snap.cik,
            factor=self.name,
            value=market_cap / net_income,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            valid=True,
            ranking_direction=self.ranking_direction,
        )


class PriceToSalesFactor:
    """FY P/S = PIT market cap / period revenue. Quarterly periods are unavailable."""

    name: str = "price_to_sales"
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "lower_is_better"
    requires_market_cap: bool = True

    def calculate(
        self,
        ticker: str,
        as_of: datetime,
        period: FinancialPeriod,
        *,
        service: FinancialsService,
        market_cap: float | None = None,
    ) -> FactorResult:
        snap = service.get_snapshot(ticker, period, as_of=as_of)
        revenue = snap.require_number("revenue")
        inputs = _market_cap_inputs(market_cap, revenue=revenue)
        filings = snapshot_accessions(snap, "revenue")
        if period.kind != "annual":
            return _annual_only(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if market_cap is None:
            return _missing_market_cap(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if market_cap <= 0:
            return _invalid_market_cap(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if revenue is None:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Revenue unavailable",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if revenue <= 0:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Revenue must be positive",
                inputs=inputs,
                source_filings=filings,
                warnings=("non_positive_revenue",),
                ranking_direction=self.ranking_direction,
            )
        return FactorResult(
            ticker=snap.ticker,
            cik=snap.cik,
            factor=self.name,
            value=market_cap / revenue,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            valid=True,
            ranking_direction=self.ranking_direction,
        )


class PriceToBookFactor:
    """P/B = PIT market cap / stockholders' equity. Book value may be quarterly."""

    name: str = "price_to_book"
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "lower_is_better"
    requires_market_cap: bool = True

    def calculate(
        self,
        ticker: str,
        as_of: datetime,
        period: FinancialPeriod,
        *,
        service: FinancialsService,
        market_cap: float | None = None,
    ) -> FactorResult:
        snap = service.get_snapshot(ticker, period, as_of=as_of)
        equity = snap.require_number("stockholders_equity")
        inputs = _market_cap_inputs(market_cap, stockholders_equity=equity)
        filings = snapshot_accessions(snap, "stockholders_equity")
        if market_cap is None:
            return _missing_market_cap(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if market_cap <= 0:
            return _invalid_market_cap(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                inputs=inputs,
                filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if equity is None:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Stockholders' equity unavailable",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if equity <= 0:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Stockholders' equity must be positive",
                inputs=inputs,
                source_filings=filings,
                warnings=("non_positive_equity",),
                ranking_direction=self.ranking_direction,
            )
        return FactorResult(
            ticker=snap.ticker,
            cik=snap.cik,
            factor=self.name,
            value=market_cap / equity,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            valid=True,
            ranking_direction=self.ranking_direction,
        )
