"""Point-in-time-safe fundamental factor calculations and ranking."""

from equitytrace.factors.models import FactorResult, RankedFactorResult, RankingResult

__all__ = [
    "FactorEngine",
    "FactorResult",
    "RankedFactorResult",
    "RankingResult",
    "get_factor",
    "list_factors",
    "rank_factors",
]


def __getattr__(name: str) -> object:
    if name == "FactorEngine":
        from equitytrace.factors.engine import FactorEngine

        return FactorEngine
    if name == "get_factor":
        from equitytrace.factors.registry import get_factor

        return get_factor
    if name == "list_factors":
        from equitytrace.factors.registry import list_factors

        return list_factors
    if name == "rank_factors":
        from equitytrace.factors.ranking import rank_factors

        return rank_factors
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
