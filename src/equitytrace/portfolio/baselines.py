"""Target-weight baselines. They do not own calendar, drift, cost, or persistence."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from equitytrace.portfolio.returns import ReturnMatrix


class BaselineUnavailable(RuntimeError):
    """Target weights cannot be formed from the selected names."""

    def __init__(self, reason: str = "baseline_unavailable") -> None:
        self.reason = reason
        super().__init__(reason)


def equal_weight(symbols: Sequence[str]) -> dict[str, float]:
    names = tuple(sorted({item.strip().upper() for item in symbols if item.strip()}))
    if not names:
        raise BaselineUnavailable("baseline_unavailable")
    weight = 1.0 / float(len(names))
    return {name: weight for name in names}


def inverse_volatility(matrix: ReturnMatrix) -> dict[str, float]:
    raw: dict[str, float] = {}
    for symbol in matrix.symbols:
        series = matrix.returns[symbol]
        if len(series) < 2:
            raise BaselineUnavailable("baseline_unavailable")
        sigma = statistics.stdev(series)
        if not math.isfinite(sigma) or sigma <= 0:
            raise BaselineUnavailable("baseline_unavailable")
        raw[symbol] = 1.0 / sigma
    total = sum(raw[symbol] for symbol in matrix.symbols)
    if not math.isfinite(total) or total <= 0:
        raise BaselineUnavailable("baseline_unavailable")
    return {symbol: raw[symbol] / total for symbol in matrix.symbols}
