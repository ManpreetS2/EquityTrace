"""Native research portfolio construction and weight-return backtesting."""

from equitytrace.portfolio.models import (
    BacktestRequest,
    BacktestResult,
    LeakageAuditResult,
    PerformanceMetrics,
    PortfolioBaseline,
    PortfolioRunStatus,
    PortfolioSchedule,
    RebalanceStatus,
)

__all__ = [
    "BacktestEngine",
    "BacktestRequest",
    "BacktestResult",
    "LeakageAuditResult",
    "PerformanceMetrics",
    "PortfolioBaseline",
    "PortfolioRunStatus",
    "PortfolioSchedule",
    "RebalanceStatus",
]


def __getattr__(name: str) -> object:
    if name == "BacktestEngine":
        from equitytrace.portfolio.engine import BacktestEngine

        return BacktestEngine
    raise AttributeError(f"module {name!r} is not exported by equitytrace.portfolio")
