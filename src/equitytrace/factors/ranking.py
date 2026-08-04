"""Cross-sectional factor ranking utilities."""

from __future__ import annotations

from datetime import datetime

from equitytrace.factors.models import FactorResult, RankedFactorResult, RankingResult
from equitytrace.factors.registry import get_factor, normalize_factor_name
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.periods import parse_period
from equitytrace.financials.service import FinancialsError, FinancialsService


def rank_factors(
    *,
    tickers: list[str],
    factor: str,
    as_of: datetime,
    period: str | FinancialPeriod,
    service: FinancialsService,
    market_cap_by_ticker: dict[str, float] | None = None,
) -> RankingResult:
    """
    Rank tickers by a factor using that factor's ranking direction.

    Invalid / unavailable results are excluded from ranks and percentiles.
    Tied values receive the same competition rank and the same percentile.
    """
    factor_name = normalize_factor_name(factor)
    factor_obj = get_factor(factor_name)
    parsed = period if isinstance(period, FinancialPeriod) else parse_period(period)
    direction = factor_obj.ranking_direction
    market_caps = market_cap_by_ticker or {}

    results: list[FactorResult] = []
    excluded: list[tuple[str, str]] = []

    for ticker in tickers:
        symbol = ticker.strip().upper()
        if not symbol:
            continue
        try:
            result = factor_obj.calculate(
                symbol,
                as_of,
                parsed,
                service=service,
                market_cap=market_caps.get(symbol),
            )
        except FinancialsError as exc:
            excluded.append((symbol, str(exc)))
            continue
        if not result.valid or result.value is None:
            excluded.append((symbol, result.unavailable_reason or "invalid result"))
            continue
        results.append(result)

    reverse = direction == "higher_is_better"
    # Deterministic secondary key on ticker for stable ordering within ties.
    results.sort(
        key=lambda r: (r.value is not None, r.value, r.ticker),
        reverse=reverse,
    )
    # When reverse=True, ticker sort is also reversed; restore A→Z within ties.
    if reverse:
        grouped: list[FactorResult] = []
        i = 0
        while i < len(results):
            j = i + 1
            while j < len(results) and results[j].value == results[i].value:
                j += 1
            tied = sorted(results[i:j], key=lambda r: r.ticker)
            grouped.extend(tied)
            i = j
        results = grouped

    n = len(results)
    ranked: list[RankedFactorResult] = []
    idx = 0
    while idx < n:
        value = results[idx].value
        end = idx + 1
        while end < n and results[end].value == value:
            end += 1
        # Competition ranking: first occurrence index + 1.
        rank = idx + 1
        percentile = 100.0 if n == 1 else 100.0 * (n - rank) / (n - 1)
        for result in results[idx:end]:
            ranked.append(
                RankedFactorResult(
                    result=result,
                    rank=rank,
                    percentile=percentile,
                )
            )
        idx = end

    return RankingResult(
        factor=factor_name,
        period=parsed,
        as_of=as_of,
        ranking_direction=direction,
        rows=tuple(ranked),
        valid_count=n,
        excluded=tuple(excluded),
    )
