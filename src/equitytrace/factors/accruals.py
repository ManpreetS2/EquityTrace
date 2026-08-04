"""Accrual quality: (net income - operating cash flow) / average total assets."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from equitytrace.factors.base import invalid_result, snapshot_accessions
from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.periods import prior_comparable_period
from equitytrace.financials.service import FinancialsService


class AccrualRatioFactor:
    """
    Accrual ratio = (net income - operating cash flow) / average total assets.

    Lower accruals (cash closer to earnings) rank better.
    """

    name: str = "accrual_ratio"
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "lower_is_better"

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
        current = service.get_snapshot(ticker, period, as_of=as_of)
        prior = service.get_snapshot(ticker, prior_comparable_period(period), as_of=as_of)

        net_income = current.require_number("net_income")
        ocf = current.require_number("operating_cash_flow")
        ending_assets = current.require_number("total_assets")
        beginning_assets = prior.require_number("total_assets")
        warnings: list[str] = []

        inputs: dict[str, float | None] = {
            "net_income": net_income,
            "operating_cash_flow": ocf,
            "ending_total_assets": ending_assets,
            "beginning_total_assets": beginning_assets,
        }
        filings = snapshot_accessions(
            current,
            "net_income",
            "operating_cash_flow",
            "total_assets",
        ) + snapshot_accessions(prior, "total_assets")

        if net_income is None or ocf is None:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Missing net income or operating cash flow",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )

        accruals = net_income - ocf
        inputs["accruals"] = accruals

        if ending_assets is not None and beginning_assets is not None:
            avg_assets = (ending_assets + beginning_assets) / 2.0
        elif ending_assets is not None:
            avg_assets = ending_assets
            warnings.append("accrual_fallback_ending_assets")
        else:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Missing total assets",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        inputs["average_total_assets"] = avg_assets
        if avg_assets == 0:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Average total assets is zero",
                inputs=inputs,
                source_filings=filings,
                warnings=("zero_denominator",),
                ranking_direction=self.ranking_direction,
            )

        return FactorResult(
            ticker=current.ticker,
            cik=current.cik,
            factor=self.name,
            value=accruals / avg_assets,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            warnings=tuple(warnings),
            valid=True,
            ranking_direction=self.ranking_direction,
        )
