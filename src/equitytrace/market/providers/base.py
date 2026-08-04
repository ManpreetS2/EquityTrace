"""Provider-neutral market-data interface."""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from equitytrace.market.models import DailyBarsResponse, PriceAdjustmentMode


@runtime_checkable
class MarketDataProvider(Protocol):
    """Fetch daily OHLCV bars from an external market-data vendor."""

    @property
    def name(self) -> str:
        """Stable provider identifier."""

    def fetch_daily_bars(
        self,
        provider_symbol: str,
        start_date: date,
        end_date: date,
        adjustment_mode: PriceAdjustmentMode,
    ) -> DailyBarsResponse:
        """
        Fetch daily bars for ``provider_symbol`` inclusive of both dates.

        Returned bars must be sorted ascending by trading date with one row
        per trading date. Non-trading calendar days are simply absent.
        """
