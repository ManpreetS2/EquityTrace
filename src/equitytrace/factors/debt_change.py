"""Debt change factor (absolute and percentage when prior debt nonzero)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from equitytrace.factors.base import invalid_result, snapshot_accessions
from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.periods import prior_comparable_period
from equitytrace.financials.service import FinancialsService


class DebtChangeFactor:
    """
    current total debt - prior comparable total debt.

    Ranking direction is lower_is_better (falling debt scores better).
    Percentage change is included in inputs when prior debt is nonzero.
    """

    name: str = "debt_change"
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
        cur_debt = current.require_number("total_debt")
        prior_debt = prior.require_number("total_debt")
        inputs: dict[str, float | None] = {
            "total_debt": cur_debt,
            "prior_total_debt": prior_debt,
        }
        filings = snapshot_accessions(current, "total_debt") + snapshot_accessions(
            prior,
            "total_debt",
        )
        if cur_debt is None or prior_debt is None:
            return invalid_result(
                ticker=current.ticker,
                cik=current.cik,
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="Missing current or prior total debt",
                inputs=inputs,
                source_filings=filings,
                ranking_direction=self.ranking_direction,
            )
        change = cur_debt - prior_debt
        if prior_debt != 0:
            inputs["debt_change_pct"] = change / prior_debt
        else:
            inputs["debt_change_pct"] = None
        return FactorResult(
            ticker=current.ticker,
            cik=current.cik,
            factor=self.name,
            value=change,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=filings,
            valid=True,
            ranking_direction=self.ranking_direction,
        )
