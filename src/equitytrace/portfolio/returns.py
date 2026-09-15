"""Common-date adjusted close-to-close return matrices for portfolio research."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from equitytrace.market.models import DailyPriceBar
from equitytrace.portfolio.models import REQUIRED_RETURNS


@dataclass(frozen=True, slots=True)
class ReturnMatrix:
    symbols: tuple[str, ...]
    dates: tuple[date, ...]
    returns: dict[str, tuple[float, ...]]


def intersect_trading_dates(*date_sets: Iterable[date]) -> list[date]:
    """Sorted intersection of price dates. Empty if any set is empty."""
    common: set[date] | None = None
    for raw in date_sets:
        current = set(raw)
        common = current if common is None else common & current
        if not common:
            return []
    if common is None:
        return []
    return sorted(common)


def pit_closes(
    bars: Sequence[DailyPriceBar],
    *,
    as_of: datetime,
    session_date: date,
) -> dict[date, float] | None:
    """Map trading date → close using only evidence known at ``as_of``."""
    closes: dict[date, float] = {}
    for bar in bars:
        if bar.available_at > as_of or bar.trading_date > session_date:
            continue
        if bar.trading_date in closes:
            return None
        close = float(bar.close)
        if not math.isfinite(close) or close <= 0:
            return None
        closes[bar.trading_date] = close
    return closes


def aligned_return_matrix(
    bars_by_symbol: Mapping[str, Sequence[DailyPriceBar]],
    *,
    as_of: datetime,
    session_date: date,
    required_returns: int = REQUIRED_RETURNS,
) -> ReturnMatrix | None:
    """Intersect price dates first, then compute identical close-to-close intervals."""
    closes_by_symbol: dict[str, dict[date, float]] = {}
    for symbol in sorted(bars_by_symbol):
        closes = pit_closes(
            bars_by_symbol[symbol],
            as_of=as_of,
            session_date=session_date,
        )
        if closes is None:
            return None
        closes_by_symbol[symbol] = closes
    if not closes_by_symbol:
        return None
    common = intersect_trading_dates(*(closes.keys() for closes in closes_by_symbol.values()))
    if len(common) < required_returns + 1:
        return None
    ordered = common[-(required_returns + 1) :]
    symbols = tuple(sorted(closes_by_symbol))
    returns: dict[str, tuple[float, ...]] = {}
    for symbol in symbols:
        prices = [closes_by_symbol[symbol][day] for day in ordered]
        series = tuple(prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices)))
        if any(not math.isfinite(value) for value in series):
            return None
        returns[symbol] = series
    return ReturnMatrix(symbols=symbols, dates=tuple(ordered), returns=returns)


def session_return(
    prev_close: float,
    curr_close: float,
) -> float | None:
    if not math.isfinite(prev_close) or not math.isfinite(curr_close):
        return None
    if prev_close <= 0 or curr_close <= 0:
        return None
    value = curr_close / prev_close - 1.0
    if not math.isfinite(value):
        return None
    return value
