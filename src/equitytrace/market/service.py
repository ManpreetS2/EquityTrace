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

        provider_enum_name = (
            (provider_name.value if provider_name else self._settings.market_data_provider)
            .strip()
            .lower()
        )
        try:
            provider_enum = MarketDataProviderName(provider_enum_name)
        except ValueError as exc:
            raise MarketDataError(
                f"Unsupported market-data provider '{provider_enum_name}'. "
                f"v0.3a supports '{MarketDataProviderName.TWELVE_DATA.value}'."
            ) from exc

        # Construct the provider before identity/mapping/run work so every later
        # exit path can close an internally owned client.
        provider, owns_provider = self._resolve_provider(provider_name)
        try:
            return self._ingest_with_provider(
                provider=provider,
                provider_enum=provider_enum,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                asset_type=asset_type,
                provider_symbol=provider_symbol,
                modes=modes,
                overlap_days=overlap_days,
            )
        finally:
            if owns_provider:
                self._close_owned_provider(provider)

    def _ingest_with_provider(
        self,
        *,
        provider: MarketDataProvider,
        provider_enum: MarketDataProviderName,
        symbol: str,
        start_date: date,
        end_date: date,
        asset_type: AssetType,
        provider_symbol: str | None,
        modes: list[PriceAdjustmentMode],
        overlap_days: int,
    ) -> MarketDataIngestionResult:
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
        modes_with_committed_rows = 0
        modes_clean = 0
        any_incomplete = False
        any_rejected = False
        today = utc_now().date()

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
                try:
                    instrument = self._repo.update_market_metadata(
                        instrument.instrument_id,
                        currency=response.currency,
                        exchange_timezone=response.exchange_timezone,
                    )
                except ValueError as exc:
                    raise MarketDataError(str(exc)) from exc

                mode_raw, mode_rejected, bars = self._prepare_bars(
                    response=response,
                    instrument_id=instrument.instrument_id,
                    provider_enum=provider_enum,
                    mode=mode,
                    today=today,
                    start_date=start_date,
                    end_date=end_date,
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
                committed = counts.inserted + counts.updated + counts.unchanged
                if committed > 0:
                    modes_with_committed_rows += 1
                    if mode_rejected == 0 and not response.incomplete:
                        modes_clean += 1
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
            except Exception:
                error_parts.append(f"{mode.value}:UnexpectedError")
                logger.exception(
                    "Unexpected market ingest failure for %s mode %s",
                    symbol,
                    mode.value,
                )

        status = _finalize_status(
            modes_requested=len(modes),
            modes_clean=modes_clean,
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
        start_date: date,
        end_date: date,
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
            if not (start_date <= provider_bar.trading_date <= end_date):
                reasons = [*reasons, "trading_date_outside_request"]
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
            # Explicit open-ended (or existing) mapping must still fully cover the range.
            self._require_complete_mapping_coverage(
                instrument_id=instrument_id,
                provider=provider,
                canonical_symbol=canonical_symbol,
                start_date=start_date,
                end_date=end_date,
            )
            return explicit

        mappings = self._repo.list_primary_mappings(instrument_id, provider)
        if not mappings:
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

        return self._require_complete_mapping_coverage(
            instrument_id=instrument_id,
            provider=provider,
            canonical_symbol=canonical_symbol,
            start_date=start_date,
            end_date=end_date,
        )

    def _require_complete_mapping_coverage(
        self,
        *,
        instrument_id: str,
        provider: MarketDataProviderName,
        canonical_symbol: str,
        start_date: date,
        end_date: date,
    ) -> str:
        """Ensure one provider symbol fully covers ``[start_date, end_date]`` with no gaps."""
        mappings = self._repo.list_primary_mappings(instrument_id, provider)
        if not mappings:
            raise MarketDataError(
                f"No provider symbol mapping covers {start_date}..{end_date} "
                f"for {canonical_symbol}."
            )

        def covers(m: MarketSymbolMapping, day: date) -> bool:
            after_start = m.valid_from is None or day >= m.valid_from
            before_end = m.valid_to is None or day <= m.valid_to
            return after_start and before_end

        start_hits = [m for m in mappings if covers(m, start_date)]
        if not start_hits:
            raise MarketDataError(
                f"No provider symbol mapping covers requested start {start_date} "
                f"for {canonical_symbol}. Ingest each validity range separately."
            )
        # Deterministic: latest valid_from wins when multiple claim start_date
        # (should be prevented by overlap invariants, but stay defensive).
        start_hits.sort(key=lambda m: (m.valid_from is None, m.valid_from or date.min))
        active = start_hits[-1]
        symbol = active.provider_symbol

        cursor = start_date
        while cursor <= end_date:
            hits = [m for m in mappings if covers(m, cursor) and m.provider_symbol == symbol]
            other = [m for m in mappings if covers(m, cursor) and m.provider_symbol != symbol]
            if other:
                details = ", ".join(
                    sorted(
                        f"{m.provider_symbol}[{m.valid_from or 'open'}..{m.valid_to or 'open'}]"
                        for m in [*hits, *other]
                    )
                )
                raise MarketDataError(
                    f"Requested range {start_date}..{end_date} spans multiple provider "
                    f"symbol mappings for {canonical_symbol}: {details}. "
                    "Ingest each validity range separately."
                )
            if not hits:
                raise MarketDataError(
                    f"Provider symbol mapping gap at {cursor} for {canonical_symbol} "
                    f"within requested range {start_date}..{end_date}. "
                    "Ingest each validity range separately."
                )
            hits.sort(key=lambda m: (m.valid_from is None, m.valid_from or date.min))
            current = hits[-1]
            if current.valid_to is None or current.valid_to >= end_date:
                return symbol
            # Advance to the day after this mapping ends; next iteration must be covered
            # by an adjacent same-symbol mapping with no gap.
            cursor = current.valid_to + timedelta(days=1)
        return symbol

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

    @staticmethod
    def _close_owned_provider(provider: MarketDataProvider) -> None:
        """Close an owned provider without masking the primary exception."""
        close = getattr(provider, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            logger.exception("Failed to close owned market-data provider")


def _finalize_status(
    *,
    modes_requested: int,
    modes_clean: int,
    modes_with_committed_rows: int,
    any_rejected: bool,
    any_incomplete: bool,
) -> MarketDataRunStatus:
    """Derive run status from committed rows, not merely successful fetches."""
    if modes_with_committed_rows == 0:
        return MarketDataRunStatus.FAILED
    if modes_clean == modes_requested and not any_rejected and not any_incomplete:
        return MarketDataRunStatus.SUCCESS
    return MarketDataRunStatus.PARTIAL
