"""Persistence repositories for EquityTrace domain objects."""

from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.filings import FilingsRepository
from equitytrace.repositories.ingestion import IngestionRepository
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.securities import SecuritiesRepository

__all__ = [
    "FactorRepository",
    "FactsRepository",
    "FilingsRepository",
    "IngestionRepository",
    "IssuersRepository",
    "MarketRepository",
    "SecuritiesRepository",
]


def __getattr__(name: str) -> object:
    if name == "FactorRepository":
        from equitytrace.repositories.factors import FactorRepository

        return FactorRepository
    if name == "MarketRepository":
        from equitytrace.repositories.market import MarketRepository

        return MarketRepository
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
