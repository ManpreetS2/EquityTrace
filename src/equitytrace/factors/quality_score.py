"""Transparent composite basic quality score (not a prediction model)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from equitytrace.factors.accruals import AccrualRatioFactor
from equitytrace.factors.base import Factor, invalid_result
from equitytrace.factors.debt_change import DebtChangeFactor
from equitytrace.factors.free_cash_flow import FreeCashFlowFactor
from equitytrace.factors.models import FactorResult
from equitytrace.factors.operating_margin import OperatingMarginChangeFactor
from equitytrace.factors.revenue_growth import RevenueGrowthFactor
from equitytrace.factors.roa import ReturnOnAssetsFactor
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.service import FinancialsService


@dataclass(frozen=True, slots=True)
class QualityWeights:
    """Configurable component weights for the basic quality score."""

    revenue_growth: float = 1.0
    operating_margin_change: float = 1.0
    free_cash_flow: float = 1.0
    debt_change: float = 1.0
    roa: float = 1.0
    accrual_ratio: float = 1.0


DEFAULT_QUALITY_WEIGHTS = QualityWeights()


class BasicQualityScoreFactor:
    """
    Transparent 0-1 style composite of available normalized component signals.

    Components (each contributes a binary good/bad signal when available):
    - positive revenue growth
    - positive operating-margin change
    - positive free cash flow
    - falling debt (negative debt change)
    - positive ROA
    - lower accrual ratio (accrual_ratio < 0 treated as good)

    Missing components are excluded from the denominator rather than invented.
    This is not a machine-learning or prediction model.
    """

    name: str = "basic_quality_score"
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"

    def __init__(self, weights: QualityWeights = DEFAULT_QUALITY_WEIGHTS) -> None:
        self.weights = weights

    def calculate(
        self,
        ticker: str,
        as_of: datetime,
        period: FinancialPeriod,
        *,
        service: FinancialsService,
        market_cap: float | None = None,
    ) -> FactorResult:
        components: list[tuple[str, Factor, float, Callable[[float], bool]]] = [
            ("revenue_growth", RevenueGrowthFactor(), self.weights.revenue_growth, _positive),
            (
                "operating_margin_change",
                OperatingMarginChangeFactor(),
                self.weights.operating_margin_change,
                _positive,
            ),
            ("free_cash_flow", FreeCashFlowFactor(), self.weights.free_cash_flow, _positive),
            ("debt_change", DebtChangeFactor(), self.weights.debt_change, _negative),
            ("roa", ReturnOnAssetsFactor(), self.weights.roa, _positive),
            ("accrual_ratio", AccrualRatioFactor(), self.weights.accrual_ratio, _negative),
        ]

        weighted_hits = 0.0
        weighted_total = 0.0
        inputs: dict[str, float | None] = {}
        filings: list[str] = []
        warnings: list[str] = []
        cik = ""
        symbol = ticker.strip().upper()

        for key, factor, weight, predicate in components:
            result = factor.calculate(
                ticker,
                as_of,
                period,
                service=service,
                market_cap=market_cap,
            )
            cik = result.cik or cik
            symbol = result.ticker or symbol
            inputs[key] = result.value
            filings.extend(result.source_filings)
            if not result.valid or result.value is None:
                warnings.append(f"quality_missing:{key}")
                continue
            weighted_total += weight
            if predicate(result.value):
                weighted_hits += weight

        if weighted_total == 0:
            return invalid_result(
                ticker=symbol,
                cik=cik or "0000000000",
                factor=self.name,
                as_of=as_of,
                period=period,
                reason="No quality components available",
                inputs=inputs,
                warnings=tuple(warnings) or ("missing_input",),
                ranking_direction=self.ranking_direction,
            )

        score = weighted_hits / weighted_total
        inputs["components_available"] = weighted_total
        inputs["components_hit"] = weighted_hits
        return FactorResult(
            ticker=symbol,
            cik=cik,
            factor=self.name,
            value=score,
            as_of=as_of,
            period=period,
            inputs=inputs,
            source_filings=tuple(dict.fromkeys(filings)),
            warnings=tuple(warnings),
            valid=True,
            ranking_direction=self.ranking_direction,
        )


def _positive(value: float) -> bool:
    return value > 0


def _negative(value: float) -> bool:
    return value < 0
