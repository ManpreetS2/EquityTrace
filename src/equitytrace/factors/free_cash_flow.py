"""Free cash flow and optional FCF yield (requires explicit market cap)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from equitytrace.factors.base import invalid_result, snapshot_accessions
from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.service import FinancialsService


class FreeCashFlowFactor:
    """Period free cash flow = operating cash flow - capital expenditures."""

    name: str = "free_cash_flow"
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"

    def calculate(
        self,
        ticker: str,
        as_of: datetime,
        period: FinancialPeriod,
        *,
        service: FinancialsService,
        market_cap: float | None = None,
    ) -> FactorResult:
        del market_cap
        snap = service.get_snapshot(ticker, period, as_of=as_of)
        fcf = snap.require_number("free_cash_flow")
        ocf = snap.require_number("operating_cash_flow")
        capex = snap.require_number("capital_expenditures")
        inputs = {
            "free_cash_flow": fcf,
            "operating_cash_flow": ocf,
            "capital_expenditures": capex,
        }
        filings = snapshot_accessions(
            snap,
            "free_cash_flow",
            "operating_cash_flow",
            "capital_expenditures",
        )
        if fcf is None:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Free cash flow unavailable (need OCF and CapEx)",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        return FactorResult(
            ticker=snap.ticker,
            cik=snap.cik,
            factor=self.name,
            value=fcf,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            valid=True,
            ranking_direction=self.ranking_direction,
        )


class FreeCashFlowYieldFactor:
    """
    Trailing / period FCF yield = free_cash_flow / market_cap.

    Callers must supply market capitalization explicitly in v0.3a. When omitted,
    returns an unavailable result (not an error). Native wiring to stored
    market-cap series is planned for v0.3b.
    """

    name: str = "fcf_yield"
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"

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
        fcf = snap.require_number("free_cash_flow")
        inputs: dict[str, float | None] = {
            "free_cash_flow": fcf,
            "market_cap": market_cap,
        }
        filings = snapshot_accessions(
            snap, "free_cash_flow", "operating_cash_flow", "capital_expenditures"
        )

        if market_cap is None:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason=(
                    "Market capitalization not supplied. "
                    "FCF yield requires an explicit market_cap argument "
                    "(native market-cap wiring arrives in v0.3b)."
                ),
                inputs=inputs,
                source_filings=filings,
                warnings=("market_cap_missing",),
                ranking_direction=self.ranking_direction,
            )
        if market_cap <= 0:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Market capitalization must be positive",
                inputs=inputs,
                source_filings=filings,
                warnings=("invalid_market_cap",),
                ranking_direction=self.ranking_direction,
            )
        if fcf is None:
            return invalid_result(
                ticker=snap.ticker,
                cik=snap.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Free cash flow unavailable",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        return FactorResult(
            ticker=snap.ticker,
            cik=snap.cik,
            factor=self.name,
            value=fcf / market_cap,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            valid=True,
            ranking_direction=self.ranking_direction,
        )
