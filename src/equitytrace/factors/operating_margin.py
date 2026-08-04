"""Operating margin level and year-over-year change."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from equitytrace.factors.base import invalid_result, snapshot_accessions
from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.periods import prior_comparable_period
from equitytrace.financials.service import FinancialsService


def _margin(operating_income: float | None, revenue: float | None) -> float | None:
    if operating_income is None or revenue is None:
        return None
    if revenue == 0:
        return None
    return operating_income / revenue


class OperatingMarginChangeFactor:
    """Current operating margin minus prior comparable operating margin."""

    name: str = "operating_margin_change"
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
        current = service.get_snapshot(ticker, period, as_of=as_of)
        prior = service.get_snapshot(ticker, prior_comparable_period(period), as_of=as_of)

        cur_op = current.require_number("operating_income")
        cur_rev = current.require_number("revenue")
        prior_op = prior.require_number("operating_income")
        prior_rev = prior.require_number("revenue")

        cur_margin = _margin(cur_op, cur_rev)
        prior_margin = _margin(prior_op, prior_rev)
        inputs = {
            "operating_income": cur_op,
            "revenue": cur_rev,
            "operating_margin": cur_margin,
            "prior_operating_income": prior_op,
            "prior_revenue": prior_rev,
            "prior_operating_margin": prior_margin,
        }
        filings = snapshot_accessions(current, "operating_income", "revenue") + snapshot_accessions(
            prior, "operating_income", "revenue"
        )

        warnings: list[str] = []
        if cur_rev == 0 or prior_rev == 0:
            warnings.append("zero_denominator")
        if cur_margin is None or prior_margin is None:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Missing inputs for operating margin change",
                inputs=inputs,
                source_filings=filings,
                warnings=tuple(warnings) or ("missing_input",),
                ranking_direction=self.ranking_direction,
            )

        return FactorResult(
            ticker=current.ticker,
            cik=current.cik,
            factor=self.name,
            value=cur_margin - prior_margin,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            warnings=tuple(warnings),
            valid=True,
            ranking_direction=self.ranking_direction,
        )
