"""Market-data ingestion service."""

from __future__ import annotations

import contextlib
import logging
from datetime import date, timedelta

import duckdb

from equitytrace.config import Settings
from equitytrace.market.availability import bar_available_at
from equitytrace.market.models import (
    AssetType,
    DailyBarsResponse,
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
        provider, owns_provider = self._resolve_provider(provider_name)
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

        try:
            instrument = self._repo.get_or_create_instrument(
                symbol,
                asset_type=asset_type,
                security_ticker=security_ticker,
                issuer_cik=issuer_cik,
                exchange=exchange,
            )
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc

        resolved_provider_symbol = self._resolve_provider_symbol(
            instrument_id=instrument.instrument_id,
            provider=provider_enum,
            canonical_symbol=instrument.canonical_symbol,
            start_date=start_date,
            end_date=end_date,
            explicit_provider_symbol=provider_symbol,
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
        error_parts: list[str] = []
        modes_succeeded = 0
        modes_with_committed_rows = 0
        any_incomplete = False
        any_rejected = False
        today = utc_now().date()

        try:
            for mode in modes:
                try:
                    fetch_start = start_date
                    stored_start, stored_end = self._repo.get_stored_date_range(
                        instrument.instrument_id,
                        adjustment_mode=mode,
                        provider=provider_enum,
                    )
                    if stored_end is not None and start_date <= stored_end:
                        fetch_start = max(
                            start_date,
                            stored_end - timedelta(days=overlap_days),
                        )
                        if start_date < (stored_start or start_date):
                            fetch_start = start_date

                    # Network I/O happens outside any DB transaction.
                    response = provider.fetch_daily_bars(
                        resolved_provider_symbol,
                        fetch_start,
                        end_date,
                        mode,
                    )
                    mode_raw, mode_rejected, bars = self._prepare_bars(
                        response=response,
                        instrument_id=instrument.instrument_id,
                        provider_enum=provider_enum,
                        mode=mode,
                        today=today,
                    )
                    totals["raw"] += mode_raw
                    totals["rejected"] += mode_rejected
                    if mode_rejected:
                        any_rejected = True
                    if response.incomplete:
                        any_incomplete = True
                        warnings.append(
                            "incomplete_interior_windows:"
                            + ",".join(
                                f"{w['window_start']}..{w['window_end']}"
                                for w in response.meta.get("incomplete_interior_windows", [])
                            )
                        )
                    if response.conflicting_duplicate_dates:
                        warnings.append(
                            "conflicting_duplicate_dates:"
                            + ",".join(d.isoformat() for d in response.conflicting_duplicate_dates)
                        )

                    counts = self._repo.upsert_price_bars_atomic(bars)
                    totals["inserted"] += counts.inserted
                    totals["updated"] += counts.updated
                    totals["unchanged"] += counts.unchanged
                    modes_succeeded += 1
                    if counts.inserted + counts.updated + counts.unchanged > 0:
                        modes_with_committed_rows += 1
                    if mode is PriceAdjustmentMode.ALL:
                        warnings.append(
                            "adjusted_series_provider_reconstructed:"
                            "adjustment_mode=all may be revised by the provider "
                            "when corporate-action history changes; "
                            "not a vendor-vintage archive"
                        )
                except MarketDataError as exc:
                    error_parts.append(f"{mode.value}:{exc}")
                    logger.info(
                        "Market ingest mode %s failed for %s: %s",
                        mode.value,
                        symbol,
                        exc,
                    )
                except Exception as exc:
                    error_parts.append(f"{mode.value}:{type(exc).__name__}")
                    logger.exception(
                        "Unexpected market ingest failure for %s mode %s",
                        symbol,
                        mode.value,
                    )
        finally:
            if owns_provider:
                close = getattr(provider, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()

        status = _finalize_status(
            modes_requested=len(modes),
            modes_succeeded=modes_succeeded,
            modes_with_committed_rows=modes_with_committed_rows,
            any_rejected=any_rejected,
            any_incomplete=any_incomplete,
        )
        error_summary = "; ".join(error_parts) if error_parts else None

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

        with contextlib.suppress(Exception):
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
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _prepare_bars(
        self,
        *,
        response: DailyBarsResponse,
        instrument_id: str,
        provider_enum: MarketDataProviderName,
        mode: PriceAdjustmentMode,
        today: date,
    ) -> tuple[int, int, list[DailyPriceBar]]:
        """Validate provider bars and return (raw_count, rejected_count, accepted_bars)."""
        rejected = response.malformed_row_count + response.duplicate_row_count
        accepted: list[DailyPriceBar] = []
        fetched_at = utc_now()
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
                rejected += 1
                continue
            try:
                available_at = bar_available_at(
                    provider_bar.trading_date,
                    provider_bar.exchange_timezone,
                )
            except MarketDataValidationError:
                rejected += 1
                continue
            accepted.append(
                DailyPriceBar(
                    instrument_id=instrument_id,
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
        raw = len(accepted) + rejected
        return raw, rejected, accepted

    def _resolve_provider_symbol(
        self,
        *,
        instrument_id: str,
        provider: MarketDataProviderName,
        canonical_symbol: str,
        start_date: date,
        end_date: date,
        explicit_provider_symbol: str | None,
    ) -> str:
        covering = self._repo.mappings_covering_range(
            instrument_id,
            provider,
            start_date=start_date,
            end_date=end_date,
        )
        symbols = {m.provider_symbol for m in covering}
        if len(symbols) > 1:
            details = ", ".join(
                sorted(
                    f"{m.provider_symbol}[{m.valid_from or 'open'}..{m.valid_to or 'open'}]"
                    for m in covering
                )
            )
            raise MarketDataError(
                f"Requested range {start_date}..{end_date} spans multiple provider "
                f"symbol mappings for {canonical_symbol}: {details}. "
                "Ingest each validity range separately."
            )

        if explicit_provider_symbol is not None:
            explicit = explicit_provider_symbol.strip()
            if not explicit:
                raise MarketDataError("provider_symbol must not be blank.")
            active = self._repo.get_active_symbol_mapping(instrument_id, provider, as_of=start_date)
            if active is not None and active.provider_symbol != explicit:
                raise MarketDataError(
                    f"Explicit provider symbol {explicit!r} conflicts with active mapping "
                    f"{active.provider_symbol!r} for {canonical_symbol} on {start_date}. "
                    "Close or date the existing mapping before introducing a transition."
                )
            if active is None or active.provider_symbol != explicit:
                try:
                    self._repo.upsert_symbol_mapping(
                        MarketSymbolMapping(
                            instrument_id=instrument_id,
                            provider=provider,
                            provider_symbol=explicit,
                            is_primary=True,
                        )
                    )
                except ValueError as exc:
                    raise MarketDataError(str(exc)) from exc
            return explicit

        if covering:
            return covering[0].provider_symbol

        # No mapping yet: create open-ended mapping from canonical symbol.
        try:
            self._repo.upsert_symbol_mapping(
                MarketSymbolMapping(
                    instrument_id=instrument_id,
                    provider=provider,
                    provider_symbol=canonical_symbol,
                    is_primary=True,
                )
            )
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc
        return canonical_symbol

    def _resolve_provider(
        self,
        provider_name: MarketDataProviderName | None,
    ) -> tuple[MarketDataProvider, bool]:
        if self._provider is not None:
            return self._provider, False
        name = provider_name.value if provider_name else self._settings.market_data_provider
        name = name.strip().lower()
        if name == MarketDataProviderName.TWELVE_DATA.value:
            return TwelveDataProvider(self._settings), True
        raise MarketDataError(
            f"Unsupported market-data provider '{name}'. "
            f"v0.3a supports '{MarketDataProviderName.TWELVE_DATA.value}'."
        )


def _finalize_status(
    *,
    modes_requested: int,
    modes_succeeded: int,
    modes_with_committed_rows: int,
    any_rejected: bool,
    any_incomplete: bool,
) -> MarketDataRunStatus:
    if modes_succeeded == 0:
        return MarketDataRunStatus.FAILED
    if modes_succeeded < modes_requested or any_rejected or any_incomplete:
        if modes_with_committed_rows > 0 or modes_succeeded > 0:
            return MarketDataRunStatus.PARTIAL
        return MarketDataRunStatus.FAILED
    return MarketDataRunStatus.SUCCESS
