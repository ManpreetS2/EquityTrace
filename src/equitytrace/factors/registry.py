"""Factor name registry and CLI alias resolution."""

from __future__ import annotations

from typing import cast

from equitytrace.factors.accruals import AccrualRatioFactor
from equitytrace.factors.base import Factor
from equitytrace.factors.debt_change import DebtChangeFactor
from equitytrace.factors.free_cash_flow import FreeCashFlowFactor, FreeCashFlowYieldFactor
from equitytrace.factors.operating_margin import OperatingMarginChangeFactor
from equitytrace.factors.quality_score import BasicQualityScoreFactor
from equitytrace.factors.revenue_growth import RevenueGrowthFactor
from equitytrace.factors.roa import ReturnOnAssetsFactor

_FACTORS: dict[str, Factor] = {
    "revenue_growth": cast(Factor, RevenueGrowthFactor()),
    "operating_margin_change": cast(Factor, OperatingMarginChangeFactor()),
    "free_cash_flow": cast(Factor, FreeCashFlowFactor()),
    "fcf_yield": cast(Factor, FreeCashFlowYieldFactor()),
    "debt_change": cast(Factor, DebtChangeFactor()),
    "roa": cast(Factor, ReturnOnAssetsFactor()),
    "accrual_ratio": cast(Factor, AccrualRatioFactor()),
    "basic_quality_score": cast(Factor, BasicQualityScoreFactor()),
}

_ALIASES = {
    "revenue-growth": "revenue_growth",
    "revenue_growth": "revenue_growth",
    "operating-margin-change": "operating_margin_change",
    "operating_margin_change": "operating_margin_change",
    "operating_margin": "operating_margin_change",
    "operating-margin": "operating_margin_change",
    "free-cash-flow": "free_cash_flow",
    "free_cash_flow": "free_cash_flow",
    "fcf": "free_cash_flow",
    "fcf-yield": "fcf_yield",
    "fcf_yield": "fcf_yield",
    "debt-change": "debt_change",
    "debt_change": "debt_change",
    "roa": "roa",
    "accrual-ratio": "accrual_ratio",
    "accrual_ratio": "accrual_ratio",
    "accruals": "accrual_ratio",
    "basic-quality-score": "basic_quality_score",
    "basic_quality_score": "basic_quality_score",
    "quality": "basic_quality_score",
    "quality-score": "basic_quality_score",
    "quality_score": "basic_quality_score",
}


class UnknownFactorError(ValueError):
    """Raised when a factor name cannot be resolved."""


def normalize_factor_name(name: str) -> str:
    """Normalize CLI / user factor names to registry keys."""
    raw = name.strip().lower().replace(" ", "-")
    underscored = raw.replace("-", "_")
    key = _ALIASES.get(raw) or _ALIASES.get(underscored)
    if key is None or key not in _FACTORS:
        raise UnknownFactorError(
            f"Unknown factor '{name}'. Known factors: {', '.join(list_factors())}"
        )
    return key


def get_factor(name: str) -> Factor:
    """Return a factor instance by name or alias."""
    return _FACTORS[normalize_factor_name(name)]


def list_factors() -> list[str]:
    """Return registered factor names in stable order."""
    return list(_FACTORS.keys())
