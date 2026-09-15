"""Compact performance statistics for a completed research backtest."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from equitytrace.portfolio.models import PerformanceMetrics, RebalanceRecord, RebalanceStatus

TRADING_DAYS_PER_YEAR = 252


def compute_metrics(
    *,
    initial_nav: float,
    final_nav: float | None,
    session_navs: Sequence[float],
    nav_events: Sequence[float],
    rebalances: Sequence[RebalanceRecord],
) -> PerformanceMetrics:
    successful = [row for row in rebalances if row.status is RebalanceStatus.SUCCESS]
    turnover = sum(row.gross_turnover for row in successful)
    costs = sum(row.cost_amount for row in successful)
    max_drawdown = _max_drawdown(nav_events)
    if final_nav is None or initial_nav <= 0 or not math.isfinite(final_nav):
        return PerformanceMetrics(
            max_drawdown=max_drawdown,
            gross_turnover=turnover,
            transaction_cost_total=costs,
            successful_rebalance_count=len(successful),
        )
    total_return = final_nav / initial_nav - 1.0
    daily = _session_returns(session_navs)
    n = len(daily)
    annualized = None
    vol = None
    sharpe = None
    if n > 0 and math.isfinite(total_return):
        annualized = (final_nav / initial_nav) ** (TRADING_DAYS_PER_YEAR / n) - 1.0
        if not math.isfinite(annualized):
            annualized = None
    if n >= 2:
        vol = statistics.stdev(daily) * math.sqrt(TRADING_DAYS_PER_YEAR)
        if not math.isfinite(vol):
            vol = None
        stdev = statistics.stdev(daily)
        if stdev > 0 and math.isfinite(stdev):
            sharpe = statistics.mean(daily) / stdev * math.sqrt(TRADING_DAYS_PER_YEAR)
            if not math.isfinite(sharpe):
                sharpe = None
    return PerformanceMetrics(
        total_return=total_return if math.isfinite(total_return) else None,
        annualized_return=annualized,
        annualized_volatility=vol,
        sharpe_ratio=sharpe,
        max_drawdown=max_drawdown,
        gross_turnover=turnover,
        transaction_cost_total=costs,
        successful_rebalance_count=len(successful),
        interval_count=n,
    )


def _session_returns(session_navs: Sequence[float]) -> list[float]:
    out: list[float] = []
    for idx in range(1, len(session_navs)):
        prev = session_navs[idx - 1]
        curr = session_navs[idx]
        if prev <= 0 or not math.isfinite(prev) or not math.isfinite(curr):
            continue
        out.append(curr / prev - 1.0)
    return out


def _max_drawdown(nav_events: Sequence[float]) -> float | None:
    peak: float | None = None
    worst: float | None = None
    for nav in nav_events:
        if not math.isfinite(nav) or nav <= 0:
            continue
        if peak is None or nav > peak:
            peak = nav
        drawdown = nav / peak - 1.0
        if worst is None or drawdown < worst:
            worst = drawdown
    return worst
