"""Point-in-time market-window analytics from stored daily bars."""

from __future__ import annotations

import math
import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import duckdb

from equitytrace.market.models import (
    DailyPriceBar,
    MarketAnalyticsResult,
    MarketDataProviderName,
    MarketMetricRankingResult,
    PriceAdjustmentMode,
    RankedMarketMetricResult,
)
from equitytrace.repositories.market import MarketRepository

REQUIRED_CLOSES = 253
REQUIRED_RETURNS = 252
MOMENTUM_SKIP_RECENT = 21
LOOKBACK_CALENDAR_DAYS = 550
TRADING_DAYS_PER_YEAR = 252
DEFAULT_BENCHMARK = "SPY"
ADJUSTED_HISTORY_WARNING = "provider_adjusted_history_not_vintage_pit"

_METRIC_ALIASES = {
    "momentum_12_1": "momentum_12_1",
    "momentum-12-1": "momentum_12_1",
    "momentum": "momentum_12_1",
    "volatility_1y": "volatility_1y",
    "volatility-1y": "volatility_1y",
    "volatility": "volatility_1y",
    "beta_1y": "beta_1y",
    "beta-1y": "beta_1y",
    "beta": "beta_1y",
    "max_drawdown_1y": "max_drawdown_1y",
    "max-drawdown-1y": "max_drawdown_1y",
    "max_drawdown": "max_drawdown_1y",
    "drawdown": "max_drawdown_1y",
}

_RANKING_DIRECTION: dict[str, Literal["higher_is_better", "lower_is_better"]] = {
    "momentum_12_1": "higher_is_better",
    "volatility_1y": "lower_is_better",
    "max_drawdown_1y": "higher_is_better",
}


class UnknownMarketMetricError(ValueError):
    """Raised when a market metric name cannot be resolved."""


class BetaHasNoRankingDirection(ValueError):
    """Beta has no default higher/lower-is-better interpretation."""


def normalize_market_metric(name: str) -> str:
    raw = name.strip().lower().replace(" ", "-")
    underscored = raw.replace("-", "_")
    key = _METRIC_ALIASES.get(raw) or _METRIC_ALIASES.get(underscored)
    if key is None:
        known = ", ".join(list_market_metrics())
        raise UnknownMarketMetricError(f"Unknown market metric '{name}'. Known: {known}")
    return key


def list_market_metrics() -> list[str]:
    return ["momentum_12_1", "volatility_1y", "beta_1y", "max_drawdown_1y"]


class MarketAnalyticsService:
    """Compute momentum, volatility, beta, and drawdown from stored adjusted closes."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._repo = MarketRepository(conn)

    def calculate(
        self,
        ticker: str,
        metric: str,
        as_of: datetime,
        *,
        benchmark: str = DEFAULT_BENCHMARK,
        provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA,
    ) -> MarketAnalyticsResult:
        as_of_utc = _as_of(as_of)
        name = normalize_market_metric(metric)
        symbol = ticker.strip().upper()
        if name == "momentum_12_1":
            return self._momentum(symbol, as_of_utc, provider)
        if name == "volatility_1y":
            return self._volatility(symbol, as_of_utc, provider)
        if name == "beta_1y":
            return self._beta(symbol, as_of_utc, provider, benchmark.strip().upper())
        return self._drawdown(symbol, as_of_utc, provider)

    def calculate_all(
        self,
        ticker: str,
        as_of: datetime,
        *,
        benchmark: str = DEFAULT_BENCHMARK,
        provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA,
    ) -> list[MarketAnalyticsResult]:
        return [
            self.calculate(ticker, name, as_of, benchmark=benchmark, provider=provider)
            for name in list_market_metrics()
        ]

    def rank(
        self,
        tickers: list[str],
        metric: str,
        as_of: datetime,
        *,
        benchmark: str = DEFAULT_BENCHMARK,
        provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA,
    ) -> MarketMetricRankingResult:
        name = normalize_market_metric(metric)
        direction = _RANKING_DIRECTION.get(name)
        if direction is None:
            raise BetaHasNoRankingDirection(
                "beta_1y has no default ranking direction; low or high beta is "
                "not universally better"
            )
        as_of_utc = _as_of(as_of)
        results: list[MarketAnalyticsResult] = []
        excluded: list[tuple[str, str]] = []
        for ticker in tickers:
            symbol = ticker.strip().upper()
            if not symbol:
                continue
            result = self.calculate(
                symbol,
                name,
                as_of_utc,
                benchmark=benchmark,
                provider=provider,
            )
            if not result.valid or result.value is None:
                excluded.append((symbol, result.unavailable_reason or "invalid result"))
                continue
            results.append(result)
        ordered = _order_for_ranking(results, higher_is_better=direction == "higher_is_better")
        n = len(ordered)
        ranked: list[RankedMarketMetricResult] = []
        idx = 0
        while idx < n:
            value = ordered[idx].value
            end = idx + 1
            while end < n and ordered[end].value == value:
                end += 1
            rank = idx + 1
            percentile = 100.0 if n == 1 else 100.0 * (n - rank) / (n - 1)
            for result in ordered[idx:end]:
                ranked.append(
                    RankedMarketMetricResult(result=result, rank=rank, percentile=percentile)
                )
            idx = end
        return MarketMetricRankingResult(
            metric=name,
            as_of=as_of_utc,
            ranking_direction=direction,
            rows=tuple(ranked),
            valid_count=n,
            excluded=tuple(excluded),
            benchmark=benchmark.strip().upper() if name == "beta_1y" else None,
        )

    def _momentum(
        self,
        ticker: str,
        as_of: datetime,
        provider: MarketDataProviderName,
    ) -> MarketAnalyticsResult:
        loaded = self._load_window(ticker, as_of, provider)
        if isinstance(loaded, MarketAnalyticsResult):
            return loaded.model_copy(update={"metric": "momentum_12_1"})
        window = loaded[-REQUIRED_CLOSES:]
        start = window[0]
        end = window[-MOMENTUM_SKIP_RECENT - 1]
        if start.close <= 0:
            return _unavailable(
                ticker,
                "momentum_12_1",
                as_of,
                provider,
                "start_close_non_positive",
                bars=window,
            )
        value = float(end.close / start.close) - 1.0
        return _valid_result(
            ticker,
            "momentum_12_1",
            as_of,
            provider,
            value,
            window_start=start.trading_date,
            window_end=end.trading_date,
            observation_count=REQUIRED_CLOSES,
            bars=window,
            ranking_direction="higher_is_better",
        )

    def _volatility(
        self,
        ticker: str,
        as_of: datetime,
        provider: MarketDataProviderName,
    ) -> MarketAnalyticsResult:
        loaded = self._load_window(ticker, as_of, provider)
        if isinstance(loaded, MarketAnalyticsResult):
            return loaded.model_copy(update={"metric": "volatility_1y"})
        window = loaded[-REQUIRED_CLOSES:]
        closes = [float(bar.close) for bar in window]
        if any(close <= 0 for close in closes):
            return _unavailable(
                ticker,
                "volatility_1y",
                as_of,
                provider,
                "non_positive_close",
                bars=window,
            )
        returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        value = statistics.stdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
        return _valid_result(
            ticker,
            "volatility_1y",
            as_of,
            provider,
            value,
            window_start=window[0].trading_date,
            window_end=window[-1].trading_date,
            observation_count=REQUIRED_CLOSES,
            bars=window,
            ranking_direction="lower_is_better",
        )

    def _beta(
        self,
        ticker: str,
        as_of: datetime,
        provider: MarketDataProviderName,
        benchmark: str,
    ) -> MarketAnalyticsResult:
        asset_bars = self._fetch_bars(ticker, as_of, provider)
        if isinstance(asset_bars, MarketAnalyticsResult):
            return asset_bars.model_copy(update={"metric": "beta_1y", "benchmark": benchmark})
        bench_bars = self._fetch_bars(benchmark, as_of, provider)
        if isinstance(bench_bars, MarketAnalyticsResult):
            reason = bench_bars.unavailable_reason or "benchmark_unavailable"
            if reason == "instrument_not_found":
                reason = "benchmark_not_found"
            return _unavailable(
                ticker,
                "beta_1y",
                as_of,
                provider,
                reason,
                bars=asset_bars,
                benchmark=benchmark,
            )
        asset_by_date = {bar.trading_date: bar for bar in asset_bars}
        bench_by_date = {bar.trading_date: bar for bar in bench_bars}
        # Intersect price dates first so both returns cover the same interval.
        common_dates = sorted(set(asset_by_date) & set(bench_by_date))
        if len(common_dates) < REQUIRED_RETURNS + 1:
            return _unavailable(
                ticker,
                "beta_1y",
                as_of,
                provider,
                "insufficient_aligned_observations",
                bars=asset_bars,
                benchmark=benchmark,
            )
        common_dates = common_dates[-(REQUIRED_RETURNS + 1) :]
        asset_prices = [float(asset_by_date[d].close) for d in common_dates]
        bench_prices = [float(bench_by_date[d].close) for d in common_dates]
        if any(price <= 0 for price in (*asset_prices, *bench_prices)):
            return _unavailable(
                ticker,
                "beta_1y",
                as_of,
                provider,
                "non_positive_close",
                bars=asset_bars,
                benchmark=benchmark,
            )
        asset_vals = [
            asset_prices[i] / asset_prices[i - 1] - 1.0 for i in range(1, len(asset_prices))
        ]
        bench_vals = [
            bench_prices[i] / bench_prices[i - 1] - 1.0 for i in range(1, len(bench_prices))
        ]
        variance = statistics.variance(bench_vals)
        if variance == 0:
            return _unavailable(
                ticker,
                "beta_1y",
                as_of,
                provider,
                "zero_benchmark_variance",
                bars=asset_bars,
                benchmark=benchmark,
            )
        value = statistics.covariance(asset_vals, bench_vals) / variance
        used = [asset_by_date[d] for d in common_dates]
        return _valid_result(
            ticker,
            "beta_1y",
            as_of,
            provider,
            value,
            window_start=common_dates[0],
            window_end=common_dates[-1],
            observation_count=REQUIRED_RETURNS,
            bars=used,
            benchmark=benchmark,
            ranking_direction=None,
        )

    def _drawdown(
        self,
        ticker: str,
        as_of: datetime,
        provider: MarketDataProviderName,
    ) -> MarketAnalyticsResult:
        loaded = self._load_window(ticker, as_of, provider)
        if isinstance(loaded, MarketAnalyticsResult):
            return loaded.model_copy(update={"metric": "max_drawdown_1y"})
        window = loaded[-REQUIRED_CLOSES:]
        if any(bar.close <= 0 for bar in window):
            return _unavailable(
                ticker,
                "max_drawdown_1y",
                as_of,
                provider,
                "non_positive_close",
                bars=window,
            )
        peak = window[0].close
        worst = 0.0
        for bar in window:
            if bar.close > peak:
                peak = bar.close
            drawdown = float(bar.close / peak) - 1.0
            if drawdown < worst:
                worst = drawdown
        return _valid_result(
            ticker,
            "max_drawdown_1y",
            as_of,
            provider,
            worst,
            window_start=window[0].trading_date,
            window_end=window[-1].trading_date,
            observation_count=REQUIRED_CLOSES,
            bars=window,
            ranking_direction="higher_is_better",
        )

    def _load_window(
        self,
        ticker: str,
        as_of: datetime,
        provider: MarketDataProviderName,
    ) -> list[DailyPriceBar] | MarketAnalyticsResult:
        fetched = self._fetch_bars(ticker, as_of, provider)
        if isinstance(fetched, MarketAnalyticsResult):
            return fetched
        if len(fetched) < REQUIRED_CLOSES:
            return _unavailable(
                ticker,
                "",
                as_of,
                provider,
                "insufficient_history",
                bars=fetched,
            )
        return fetched

    def _fetch_bars(
        self,
        ticker: str,
        as_of: datetime,
        provider: MarketDataProviderName,
    ) -> list[DailyPriceBar] | MarketAnalyticsResult:
        instrument = self._repo.get_instrument_by_symbol(ticker)
        if instrument is None:
            return _unavailable(ticker, "", as_of, provider, "instrument_not_found")
        start = as_of.date() - timedelta(days=LOOKBACK_CALENDAR_DAYS)
        bars = self._repo.get_price_bars(
            instrument.instrument_id,
            start_date=start,
            end_date=as_of.date(),
            adjustment_mode=PriceAdjustmentMode.ALL,
            provider=provider,
            as_of=as_of,
        )
        dates = [bar.trading_date for bar in bars]
        if len(dates) != len(set(dates)):
            return _unavailable(
                ticker,
                "",
                as_of,
                provider,
                "ambiguous_provider_history",
                bars=bars,
            )
        return bars


def _as_of(as_of: datetime) -> datetime:
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=UTC)
    return as_of.astimezone(UTC)


def _unavailable(
    ticker: str,
    metric: str,
    as_of: datetime,
    provider: MarketDataProviderName,
    reason: str,
    *,
    bars: list[DailyPriceBar] | None = None,
    benchmark: str | None = None,
) -> MarketAnalyticsResult:
    used = bars or []
    return MarketAnalyticsResult(
        ticker=ticker,
        metric=metric,
        value=None,
        as_of=as_of,
        valid=False,
        unavailable_reason=reason,
        observation_count=len(used),
        provider=provider,
        adjustment_mode=PriceAdjustmentMode.ALL,
        benchmark=benchmark,
        warnings=(ADJUSTED_HISTORY_WARNING,),
        first_trading_date=used[0].trading_date if used else None,
        last_trading_date=used[-1].trading_date if used else None,
        latest_available_at=max(bar.available_at for bar in used) if used else None,
        fetched_at_min=min(bar.fetched_at for bar in used) if used else None,
        fetched_at_max=max(bar.fetched_at for bar in used) if used else None,
        ranking_direction=_RANKING_DIRECTION.get(metric),
    )


def _valid_result(
    ticker: str,
    metric: str,
    as_of: datetime,
    provider: MarketDataProviderName,
    value: float,
    *,
    window_start: date,
    window_end: date,
    observation_count: int,
    bars: list[DailyPriceBar],
    benchmark: str | None = None,
    ranking_direction: Literal["higher_is_better", "lower_is_better"] | None,
) -> MarketAnalyticsResult:
    return MarketAnalyticsResult(
        ticker=ticker,
        metric=metric,
        value=value,
        as_of=as_of,
        valid=True,
        window_start=window_start,
        window_end=window_end,
        observation_count=observation_count,
        provider=provider,
        adjustment_mode=PriceAdjustmentMode.ALL,
        benchmark=benchmark,
        warnings=(ADJUSTED_HISTORY_WARNING,),
        first_trading_date=bars[0].trading_date if bars else None,
        last_trading_date=bars[-1].trading_date if bars else None,
        latest_available_at=max(bar.available_at for bar in bars) if bars else None,
        fetched_at_min=min(bar.fetched_at for bar in bars) if bars else None,
        fetched_at_max=max(bar.fetched_at for bar in bars) if bars else None,
        ranking_direction=ranking_direction,
    )


def _order_for_ranking(
    results: list[MarketAnalyticsResult],
    *,
    higher_is_better: bool,
) -> list[MarketAnalyticsResult]:
    ordered = list(results)
    ordered.sort(
        key=lambda r: (r.value is not None, r.value, r.ticker),
        reverse=higher_is_better,
    )
    if higher_is_better:
        grouped: list[MarketAnalyticsResult] = []
        i = 0
        while i < len(ordered):
            j = i + 1
            while j < len(ordered) and ordered[j].value == ordered[i].value:
                j += 1
            tied = sorted(ordered[i:j], key=lambda r: r.ticker)
            grouped.extend(tied)
            i = j
        ordered = grouped
    return ordered
