"""Historically safe market-capitalization calculations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

import duckdb

from equitytrace.market.models import (
    MarketCapResult,
    MarketCapSeriesPoint,
    PriceAdjustmentMode,
)
from equitytrace.market.shares import SharesOutstandingService
from equitytrace.repositories.market import MarketRepository


class MarketCapFrequency(StrEnum):
    DAILY = "daily"
    MONTH_END = "month-end"
    QUARTER_END = "quarter-end"


DEFAULT_MAX_PRICE_STALENESS_DAYS = 7


class MarketCapService:
    """Compute market cap from raw close * point-in-time shares outstanding."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._repo = MarketRepository(conn)
        self._shares = SharesOutstandingService(conn)

    def get_market_cap(
        self,
        ticker: str,
        market_date: date,
        *,
        as_of: datetime | None = None,
        max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    ) -> MarketCapResult:
        symbol = ticker.strip().upper()
        warnings: list[str] = []

        instrument = self._repo.get_instrument_by_symbol(symbol)
        if instrument is None:
            return MarketCapResult(
                ticker=symbol,
                market_date_requested=market_date,
                unavailable_reason="instrument_not_found",
            )

        # Multi-class ambiguity: refuse falsely precise class-level market caps.
        if instrument.issuer_cik:
            sec_count = self._repo.count_securities_for_issuer(instrument.issuer_cik)
            if sec_count > 1:
                return MarketCapResult(
                    ticker=symbol,
                    market_date_requested=market_date,
                    unavailable_reason="multi_class_issuer_ambiguous",
                    warnings=(
                        "multi_class_issuer:"
                        f"issuer {instrument.issuer_cik} has {sec_count} securities; "
                        "refusing to multiply one class price by potentially aggregate shares",
                    ),
                )

        # Price selection uses raw (adjustment_mode=none) only.
        # When an explicit knowledge time is provided, availability filters apply.
        price = None
        if as_of is not None:
            knowledge_filter = (as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)).astimezone(
                UTC
            )
            price = self._repo.get_latest_price_on_or_before(
                instrument.instrument_id,
                market_date,
                adjustment_mode=PriceAdjustmentMode.NONE,
                as_of=knowledge_filter,
            )
        else:
            bars = self._repo.get_price_bars(
                instrument.instrument_id,
                start_date=market_date,
                end_date=market_date,
                adjustment_mode=PriceAdjustmentMode.NONE,
            )
            price = (
                bars[0]
                if bars
                else self._repo.get_latest_price_on_or_before(
                    instrument.instrument_id,
                    market_date,
                    adjustment_mode=PriceAdjustmentMode.NONE,
                )
            )
        if price is None:
            return MarketCapResult(
                ticker=symbol,
                market_date_requested=market_date,
                unavailable_reason="missing_raw_price",
            )

        staleness = (market_date - price.trading_date).days
        if staleness > max_price_staleness_days:
            return MarketCapResult(
                ticker=symbol,
                market_date_requested=market_date,
                price_date_used=price.trading_date,
                unavailable_reason=(
                    f"price_stale:{staleness}_days_exceeds_{max_price_staleness_days}"
                ),
            )
        if price.trading_date != market_date:
            warnings.append(f"used_prior_trading_day:{price.trading_date.isoformat()}")

        # Ensure market-cap never uses a price from after the requested market date.
        if price.trading_date > market_date:
            return MarketCapResult(
                ticker=symbol,
                market_date_requested=market_date,
                unavailable_reason="price_after_market_date",
            )

        knowledge_time = as_of if as_of is not None else price.available_at
        knowledge_time = (
            knowledge_time if knowledge_time.tzinfo else knowledge_time.replace(tzinfo=UTC)
        ).astimezone(UTC)
        if as_of is not None and knowledge_time > price.available_at.astimezone(UTC):
            warnings.append("later_knowledge_time")

        # Ensure the selected price itself is known by the knowledge time.
        if price.available_at.astimezone(UTC) > knowledge_time:
            return MarketCapResult(
                ticker=symbol,
                market_date_requested=market_date,
                price_date_used=price.trading_date,
                knowledge_time=knowledge_time,
                unavailable_reason="price_unavailable_at_knowledge_time",
                warnings=tuple(warnings),
            )

        shares = self._shares.get_shares_outstanding(
            symbol,
            market_date=price.trading_date,
            knowledge_time=knowledge_time,
        )
        warnings.extend(shares.warnings)
        if not shares.is_available or shares.shares is None:
            return MarketCapResult(
                ticker=symbol,
                market_date_requested=market_date,
                price_date_used=price.trading_date,
                knowledge_time=knowledge_time,
                raw_close=price.close,
                currency=price.currency,
                price_provider=price.provider,
                price_adjustment_mode=price.adjustment_mode,
                price_fetched_at=price.fetched_at,
                unavailable_reason=shares.unavailable_reason or "missing_shares_outstanding",
                warnings=tuple(dict.fromkeys(warnings)),
            )

        market_cap = price.close * shares.shares
        return MarketCapResult(
            ticker=symbol,
            market_date_requested=market_date,
            price_date_used=price.trading_date,
            knowledge_time=knowledge_time,
            raw_close=price.close,
            currency=price.currency,
            shares_outstanding=shares.shares,
            shares_fact_date=shares.fact_date,
            shares_available_at=shares.available_at,
            market_cap=market_cap,
            price_provider=price.provider,
            price_adjustment_mode=PriceAdjustmentMode.NONE,
            price_fetched_at=price.fetched_at,
            sec_concept=shares.concept,
            sec_accession=shares.accession_number,
            sec_form=shares.form,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def get_market_cap_series(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        *,
        frequency: MarketCapFrequency = MarketCapFrequency.DAILY,
        max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    ) -> list[MarketCapSeriesPoint]:
        if start_date > end_date:
            return []
        symbol = ticker.strip().upper()
        instrument = self._repo.get_instrument_by_symbol(symbol)
        if instrument is None:
            return []

        bars = self._repo.get_price_bars(
            instrument.instrument_id,
            start_date=start_date,
            end_date=end_date,
            adjustment_mode=PriceAdjustmentMode.NONE,
        )
        if not bars:
            return []

        selected_dates = _select_series_dates(
            [b.trading_date for b in bars],
            frequency=frequency,
        )
        points: list[MarketCapSeriesPoint] = []
        for d in selected_dates:
            result = self.get_market_cap(
                symbol,
                d,
                max_price_staleness_days=max_price_staleness_days,
            )
            points.append(MarketCapSeriesPoint(as_of_date=d, result=result))
        return points


def _select_series_dates(
    trading_dates: list[date],
    *,
    frequency: MarketCapFrequency,
) -> list[date]:
    if frequency is MarketCapFrequency.DAILY:
        return list(trading_dates)

    by_bucket: dict[tuple[int, int], date] = {}
    for d in trading_dates:
        if frequency is MarketCapFrequency.MONTH_END:
            key = (d.year, d.month)
        else:
            quarter = (d.month - 1) // 3 + 1
            key = (d.year, quarter)
        prev = by_bucket.get(key)
        if prev is None or d > prev:
            by_bucket[key] = d
    return [by_bucket[k] for k in sorted(by_bucket)]
