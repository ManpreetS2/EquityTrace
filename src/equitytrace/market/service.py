"""Market-data ingestion service."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

from equitytrace.config import Settings
from equitytrace.market.availability import bar_available_at
from equitytrace.market.models import (
    AssetType,
    DailyPriceBar,
    MarketDataIngestionResult,
    MarketDataProviderName,
    MarketDataRunStatus,
    MarketSymbolMapping,
    PriceAdjustmentMode,
    metadata_json,
)
from equitytrace.market.providers.base import MarketDataProvider
from equitytrace.market.providers.errors import MarketDataError, MarketDataValidationError
from equitytrace.market.providers.twelve_data import TwelveDataProvider, validate_ohlcv
from equitytrace.models import utc_now
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository

logger = logging.getLogger(__name__)

DEFAULT_OVERLAP_DAYS = 5


class MarketDataService:
    """Orchestrate provider fetch, validation, and DuckDB persistence."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        settings: Settings,
        *,
        provider: MarketDataProvider | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._repo = MarketRepository(conn)
        self._securities = SecuritiesRepository(conn)
        self._provider = provider

    def ingest(
        self,
        symbol: str,
        *,
        start_date: date,
        end_date: date,
        asset_type: AssetType = AssetType.EQUITY,
        provider_name: MarketDataProviderName | None = None,
        provider_symbol: str | None = None,
        modes: list[PriceAdjustmentMode] | None = None,
        overlap_days: int = DEFAULT_OVERLAP_DAYS,
    ) -> MarketDataIngestionResult:
        if start_date > end_date:
            raise MarketDataError(f"start_date {start_date} is after end_date {end_date}.")
        if overlap_days < 0:
            raise MarketDataError("overlap_days must be >= 0.")

        modes = modes or [PriceAdjustmentMode.NONE, PriceAdjustmentMode.ALL]
        provider = self._resolve_provider(provider_name)
        provider_enum = MarketDataProviderName(provider.name)

        security_ticker: str | None = None
        issuer_cik: str | None = None
        exchange: str | None = None
        if asset_type is AssetType.EQUITY:
            matches = self._securities.get_by_ticker(symbol)
            if matches:
                ciks = {m.cik for m in matches}
                if len(ciks) > 1:
                    raise MarketDataError(
                        f"Ambiguous security match for {symbol.strip().upper()}: "
                        f"multiple CIKs {', '.join(sorted(ciks))}."
                    )
                primary = next((m for m in matches if m.is_primary), matches[0])
                security_ticker = primary.ticker
                issuer_cik = primary.cik
                exchange = primary.exchange

        instrument = self._repo.get_or_create_instrument(
            symbol,
            asset_type=asset_type,
            security_ticker=security_ticker,
            issuer_cik=issuer_cik,
            exchange=exchange,
        )
        # Do not overwrite existing non-blank identity metadata with blanks.
        if (issuer_cik and instrument.issuer_cik and instrument.issuer_cik != issuer_cik) or (
            security_ticker
            and instrument.security_ticker
            and instrument.security_ticker != security_ticker
        ):
            raise MarketDataError(
                f"Instrument {instrument.canonical_symbol} is already linked to a different "
                f"security/issuer identity."
            )

        resolved_provider_symbol = (provider_symbol or symbol).strip()
        self._repo.upsert_symbol_mapping(
            MarketSymbolMapping(
                instrument_id=instrument.instrument_id,
                provider=provider_enum,
                provider_symbol=resolved_provider_symbol,
                is_primary=True,
            )
        )

        run_id = self._repo.create_market_data_run(
            provider=provider_enum,
            instrument_id=instrument.instrument_id,
            requested_start_date=start_date,
            requested_end_date=end_date,
            adjustment_modes=modes,
        )

        totals = {
            "raw": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "rejected": 0,
        }
        warnings: list[str] = []
        error_summary: str | None = None
        status = MarketDataRunStatus.SUCCESS
        today = utc_now().date()

        try:
            for mode in modes:
                fetch_start = start_date
                stored_start, stored_end = self._repo.get_stored_date_range(
                    instrument.instrument_id,
                    adjustment_mode=mode,
                    provider=provider_enum,
                )
                if stored_end is not None and start_date <= stored_end:
                    # Incremental: overlap then continue forward.
                    fetch_start = max(
                        start_date,
                        stored_end - timedelta(days=overlap_days),
                    )
                    if start_date < (stored_start or start_date):
                        # Explicit historical backfill before stored range.
                        fetch_start = start_date

                response = provider.fetch_daily_bars(
                    resolved_provider_symbol,
                    fetch_start,
                    end_date,
                    mode,
                )
                totals["raw"] += len(response.bars) + response.malformed_row_count
                totals["rejected"] += response.malformed_row_count
                fetched_at = utc_now()
                bars: list[DailyPriceBar] = []
                for provider_bar in response.bars:
                    reasons = validate_ohlcv(
                        open_=provider_bar.open,
                        high=provider_bar.high,
                        low=provider_bar.low,
                        close=provider_bar.close,
                        volume=provider_bar.volume,
                    )
                    if provider_bar.trading_date > today:
                        reasons = [*reasons, "future_trading_date"]
                    if reasons:
                        totals["rejected"] += 1
                        continue
                    try:
                        available_at = bar_available_at(
                            provider_bar.trading_date,
                            provider_bar.exchange_timezone,
                        )
                    except MarketDataValidationError:
                        totals["rejected"] += 1
                        continue
                    bars.append(
                        DailyPriceBar(
                            instrument_id=instrument.instrument_id,
                            provider=provider_enum,
                            trading_date=provider_bar.trading_date,
                            adjustment_mode=mode,
                            open=provider_bar.open,
                            high=provider_bar.high,
                            low=provider_bar.low,
                            close=provider_bar.close,
                            volume=provider_bar.volume,
                            currency=provider_bar.currency,
                            exchange_timezone=provider_bar.exchange_timezone,
                            available_at=available_at,
                            fetched_at=fetched_at,
                            source_metadata_json=metadata_json(provider_bar.metadata),
                        )
                    )
                counts = self._repo.upsert_price_bars(bars)
                totals["inserted"] += counts.inserted
                totals["updated"] += counts.updated
                totals["unchanged"] += counts.unchanged
                totals["rejected"] += counts.rejected
                if mode is PriceAdjustmentMode.ALL:
                    warnings.append(
                        "adjusted_series_provider_reconstructed:"
                        "adjustment_mode=all may be revised by the provider "
                        "when corporate-action history changes; not a vendor-vintage archive"
                    )
        except MarketDataError as exc:
            status = MarketDataRunStatus.FAILED
            error_summary = str(exc)
            logger.info("Market ingest failed for %s: %s", symbol, error_summary)
        except Exception as exc:
            status = MarketDataRunStatus.FAILED
            error_summary = f"Unexpected market ingest failure: {type(exc).__name__}"
            logger.exception("Unexpected market ingest failure for %s", symbol)

        if status is MarketDataRunStatus.SUCCESS and totals["rejected"] and totals["inserted"] == 0:
            status = MarketDataRunStatus.PARTIAL
        if status is MarketDataRunStatus.SUCCESS and totals["raw"] == 0:
            status = MarketDataRunStatus.PARTIAL

        overall_start: date | None = None
        overall_end: date | None = None
        for mode in modes:
            s, e = self._repo.get_stored_date_range(
                instrument.instrument_id,
                adjustment_mode=mode,
                provider=provider_enum,
            )
            if s is not None:
                overall_start = s if overall_start is None else min(overall_start, s)
            if e is not None:
                overall_end = e if overall_end is None else max(overall_end, e)

        self._repo.complete_market_data_run(
            run_id,
            status=status,
            raw_row_count=totals["raw"],
            inserted_row_count=totals["inserted"],
            updated_row_count=totals["updated"],
            unchanged_row_count=totals["unchanged"],
            rejected_row_count=totals["rejected"],
            error_summary=error_summary,
        )
        return MarketDataIngestionResult(
            run_id=run_id,
            provider=provider_enum,
            instrument_id=instrument.instrument_id,
            canonical_symbol=instrument.canonical_symbol,
            provider_symbol=resolved_provider_symbol,
            requested_start_date=start_date,
            requested_end_date=end_date,
            adjustment_modes=tuple(modes),
            status=status,
            raw_row_count=totals["raw"],
            inserted_row_count=totals["inserted"],
            updated_row_count=totals["updated"],
            unchanged_row_count=totals["unchanged"],
            rejected_row_count=totals["rejected"],
            stored_start_date=overall_start,
            stored_end_date=overall_end,
            error_summary=error_summary,
            warnings=tuple(warnings),
        )

    def _resolve_provider(
        self,
        provider_name: MarketDataProviderName | None,
    ) -> MarketDataProvider:
        if self._provider is not None:
            return self._provider
        name = provider_name.value if provider_name else self._settings.market_data_provider
        name = name.strip().lower()
        if name == MarketDataProviderName.TWELVE_DATA.value:
            return TwelveDataProvider(self._settings)
        raise MarketDataError(
            f"Unsupported market-data provider '{name}'. "
            f"v0.3a supports '{MarketDataProviderName.TWELVE_DATA.value}'."
        )
