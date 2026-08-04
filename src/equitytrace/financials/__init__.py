"""Canonical financial statements and point-in-time snapshots."""

from equitytrace.financials.models import (
    CanonicalValue,
    FinancialPeriod,
    FinancialSnapshot,
    ValueProvenance,
)
from equitytrace.financials.periods import parse_period, prior_comparable_period

__all__ = [
    "CanonicalValue",
    "FinancialPeriod",
    "FinancialSnapshot",
    "ValueProvenance",
    "parse_period",
    "prior_comparable_period",
]


def __getattr__(name: str) -> object:
    if name == "FinancialsService":
        from equitytrace.financials.service import FinancialsService

        return FinancialsService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
