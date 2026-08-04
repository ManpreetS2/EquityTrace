"""Year-over-year revenue growth factor."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from equitytrace.factors.base import invalid_result, snapshot_accessions
from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.periods import duration_days, prior_comparable_period
from equitytrace.financials.service import FinancialsService

# Material duration gap for annual 52- vs 53-week comparisons (~1 week).
_ANNUAL_DURATION_WARN_DAYS = 5
_ANNUAL_DURATION_REJECT_DAYS = 40


class RevenueGrowthFactor:
    """(current revenue / prior comparable revenue) - 1."""

    name: str = "revenue_growth"
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
        prior_period = prior_comparable_period(period)
        prior = service.get_snapshot(ticker, prior_period, as_of=as_of)

        if current.period.kind != prior.period.kind:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Mismatched period kinds",
                warnings=("period_mismatch",),
            )
        if current.period.fiscal_period != prior.period.fiscal_period:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Mismatched fiscal period labels",
                warnings=("period_mismatch",),
            )

        cur_rev = current.require_number("revenue")
        prior_rev = prior.require_number("revenue")
        inputs = {"revenue": cur_rev, "prior_revenue": prior_rev}
        filings = snapshot_accessions(current, "revenue") + snapshot_accessions(prior, "revenue")
        warnings: list[str] = []

        if cur_rev is None or prior_rev is None:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Missing current or prior revenue",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        if prior_rev == 0:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Prior revenue is zero",
                inputs=inputs,
                source_filings=filings,
                warnings=("zero_denominator",),
                ranking_direction=self.ranking_direction,
            )

        cur_item = current.get_value("revenue")
        prior_item = prior.get_value("revenue")
        if cur_item and prior_item:
            if cur_item.unit and prior_item.unit and cur_item.unit != prior_item.unit:
                return invalid_result(
                    ticker=current.ticker,
                    cik=current.cik,
                    factor=self.name,
                    as_of=as_of,
                    period=period,
                    reason="Revenue units differ across periods",
                    inputs=inputs,
                    source_filings=filings,
                    warnings=("unit_mismatch",),
                    ranking_direction=self.ranking_direction,
                )
            cur_days = duration_days(
                cur_item.provenance.period_start,
                cur_item.provenance.period_end,
            )
            prior_days = duration_days(
                prior_item.provenance.period_start,
                prior_item.provenance.period_end,
            )
            if cur_days is not None and prior_days is not None:
                gap = abs(cur_days - prior_days)
                inputs["current_duration_days"] = float(cur_days)
                inputs["prior_duration_days"] = float(prior_days)
                if gap > _ANNUAL_DURATION_REJECT_DAYS:
                    return invalid_result(
                        ticker=current.ticker,
                        cik=current.cik,
                        factor=self.name,
                        as_of=as_of,
                        period=period,
                        reason=(
                            f"Revenue durations differ by {gap} days "
                            f"(reject threshold {_ANNUAL_DURATION_REJECT_DAYS})"
                        ),
                        inputs=inputs,
                        source_filings=filings,
                        warnings=("period_mismatch", "duration_mismatch"),
                        ranking_direction=self.ranking_direction,
                    )
                if gap > _ANNUAL_DURATION_WARN_DAYS:
                    warnings.append("duration_mismatch_53_week")

        return FactorResult(
            ticker=current.ticker,
            cik=current.cik,
            factor=self.name,
            value=(cur_rev / prior_rev) - 1.0,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            warnings=tuple(warnings),
            valid=True,
            ranking_direction=self.ranking_direction,
        )
