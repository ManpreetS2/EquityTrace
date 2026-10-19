"""Market instrument, mapping, price-bar, and run persistence."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb

from equitytrace.market.models import (
    AssetType,
    DailyPriceBar,
    MarketDataProviderName,
    MarketDataRunStatus,
    MarketInstrument,
    MarketSymbolMapping,
    PriceAdjustmentMode,
    SplitCoverageStatus,
    StockSplitCoverage,
    StockSplitEvent,
    make_instrument_id,
    make_mapping_id,
    make_split_coverage_id,
    make_split_event_id,
    metadata_json,
)
from equitytrace.models import utc_now as _utc_now


class UpsertCounts:
    """Counts for an idempotent price-bar upsert."""

    def __init__(self) -> None:
        self.inserted = 0
        self.updated = 0
        self.unchanged = 0
        self.rejected = 0


class MarketRepository:
    """Read/write access to market-data tables."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def get_or_create_instrument(
        self,
        canonical_symbol: str,
        *,
        asset_type: AssetType = AssetType.EQUITY,
        security_ticker: str | None = None,
        issuer_cik: str | None = None,
        exchange: str | None = None,
        currency: str = "USD",
        exchange_timezone: str = "America/New_York",
    ) -> MarketInstrument:
        symbol = canonical_symbol.strip().upper()
        existing = self.get_instrument_by_symbol(symbol)
        if existing is not None:
            return self.enrich_instrument(
                existing,
                security_ticker=security_ticker,
                issuer_cik=issuer_cik,
                exchange=exchange,
            )

        instrument = MarketInstrument(
            instrument_id=make_instrument_id(symbol),
            canonical_symbol=symbol,
            asset_type=asset_type,
            security_ticker=security_ticker or (symbol if asset_type is AssetType.EQUITY else None),
            issuer_cik=issuer_cik,
            exchange=exchange,
            currency=currency,
            exchange_timezone=exchange_timezone,
            market_metadata_confirmed=False,
        )
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO market_instruments (
                instrument_id, canonical_symbol, asset_type, security_ticker, issuer_cik,
                exchange, mic_code, currency, exchange_timezone, market_metadata_confirmed,
                active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (instrument_id) DO NOTHING
            """,
            [
                instrument.instrument_id,
                instrument.canonical_symbol,
                instrument.asset_type.value,
                instrument.security_ticker,
                instrument.issuer_cik,
                instrument.exchange,
                instrument.mic_code,
                instrument.currency,
                instrument.exchange_timezone,
                instrument.market_metadata_confirmed,
                instrument.active,
                now,
                now,
            ],
        )
        # Unique on canonical_symbol may win a race; re-read.
        stored = self.get_instrument_by_symbol(symbol)
        if stored is None:
            raise RuntimeError(f"Failed to persist instrument {symbol}")
        return stored

    def enrich_instrument(
        self,
        instrument: MarketInstrument,
        *,
        security_ticker: str | None = None,
        issuer_cik: str | None = None,
        exchange: str | None = None,
    ) -> MarketInstrument:
        """Fill null identity fields from trusted SEC data; never overwrite conflicts."""

        def _norm(value: str | None) -> str | None:
            if value is None:
                return None
            text = value.strip()
            return text or None

        new_ticker = _norm(security_ticker)
        new_cik = _norm(issuer_cik)
        new_exchange = _norm(exchange)

        if instrument.issuer_cik and new_cik and instrument.issuer_cik != new_cik:
            raise ValueError(
                f"Instrument {instrument.canonical_symbol} is linked to CIK "
                f"{instrument.issuer_cik}, cannot enrich with {new_cik}."
            )
        if instrument.security_ticker and new_ticker and instrument.security_ticker != new_ticker:
            raise ValueError(
                f"Instrument {instrument.canonical_symbol} is linked to security "
                f"{instrument.security_ticker}, cannot enrich with {new_ticker}."
            )
        if instrument.exchange and new_exchange and instrument.exchange != new_exchange:
            raise ValueError(
                f"Instrument {instrument.canonical_symbol} is linked to exchange "
                f"{instrument.exchange}, cannot enrich with {new_exchange}."
            )

        next_ticker = instrument.security_ticker or new_ticker
        next_cik = instrument.issuer_cik or new_cik
        next_exchange = instrument.exchange or new_exchange
        if (
            next_ticker == instrument.security_ticker
            and next_cik == instrument.issuer_cik
            and next_exchange == instrument.exchange
        ):
            return instrument

        now = _utc_now()
        self._conn.execute(
            """
            UPDATE market_instruments SET
                security_ticker = ?,
                issuer_cik = ?,
                exchange = ?,
                updated_at = ?
            WHERE instrument_id = ?
            """,
            [next_ticker, next_cik, next_exchange, now, instrument.instrument_id],
        )
        refreshed = self.get_instrument(instrument.instrument_id)
        if refreshed is None:
            raise RuntimeError(f"Failed to refresh instrument {instrument.instrument_id}")
        return refreshed

    def get_instrument_by_symbol(self, canonical_symbol: str) -> MarketInstrument | None:
        row = self._conn.execute(
            """
            SELECT instrument_id, canonical_symbol, asset_type, security_ticker, issuer_cik,
                   exchange, mic_code, currency, exchange_timezone, market_metadata_confirmed,
                   active, created_at, updated_at
            FROM market_instruments
            WHERE canonical_symbol = ?
            """,
            [canonical_symbol.strip().upper()],
        ).fetchone()
        return _row_to_instrument(row) if row else None

    def get_instruments_by_symbols(
        self,
        symbols: Sequence[str],
    ) -> dict[str, MarketInstrument]:
        """Load instruments for canonical symbols in one query."""
        cleaned = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not cleaned:
            return {}
        placeholders = ", ".join("?" * len(cleaned))
        rows = self._conn.execute(
            f"""
            SELECT instrument_id, canonical_symbol, asset_type, security_ticker, issuer_cik,
                   exchange, mic_code, currency, exchange_timezone, market_metadata_confirmed,
                   active, created_at, updated_at
            FROM market_instruments
            WHERE canonical_symbol IN ({placeholders})
            """,
            cleaned,
        ).fetchall()
        instruments = [_row_to_instrument(row) for row in rows]
        return {instrument.canonical_symbol: instrument for instrument in instruments}

    def issuer_ciks_for_symbols(self, symbols: Sequence[str]) -> dict[str, str]:
        """Map research symbols to issuer CIK from instruments, else securities."""
        cleaned = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not cleaned:
            return {}
        instruments = self.get_instruments_by_symbols(cleaned)
        out: dict[str, str] = {}
        missing: list[str] = []
        for symbol in cleaned:
            instrument = instruments.get(symbol)
            cik = instrument.issuer_cik if instrument is not None else None
            if cik:
                out[symbol] = cik
            else:
                missing.append(symbol)
        for symbol in missing:
            row = self._conn.execute(
                """
                SELECT cik FROM securities
                WHERE ticker = ?
                ORDER BY is_primary DESC, cik
                LIMIT 1
                """,
                [symbol],
            ).fetchone()
            if row is not None and row[0] is not None:
                out[symbol] = str(row[0])
        return out

    def get_instrument(self, instrument_id: str) -> MarketInstrument | None:
        row = self._conn.execute(
            """
            SELECT instrument_id, canonical_symbol, asset_type, security_ticker, issuer_cik,
                   exchange, mic_code, currency, exchange_timezone, market_metadata_confirmed,
                   active, created_at, updated_at
            FROM market_instruments
            WHERE instrument_id = ?
            """,
            [instrument_id],
        ).fetchone()
        return _row_to_instrument(row) if row else None

    def update_market_metadata(
        self,
        instrument_id: str,
        *,
        currency: str,
        exchange_timezone: str,
    ) -> MarketInstrument:
        """Fill or confirm currency/timezone from a trusted provider response.

        Placeholder policy: until ``market_metadata_confirmed`` is true, the
        creation defaults (USD / America/New_York) are placeholders and may be
        replaced by the first trusted provider values. Once confirmed, later
        responses must match exactly; conflicts raise rather than overwrite.
        """
        instrument = self.get_instrument(instrument_id)
        if instrument is None:
            raise ValueError(f"Unknown instrument_id {instrument_id!r}.")

        currency_norm = currency.strip().upper()
        tz_norm = exchange_timezone.strip()
        if not currency_norm:
            raise ValueError("currency must not be blank.")
        if not tz_norm:
            raise ValueError("exchange_timezone must not be blank.")
        try:
            ZoneInfo(tz_norm)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown exchange timezone '{tz_norm}'.") from exc

        if instrument.market_metadata_confirmed:
            if instrument.currency != currency_norm or instrument.exchange_timezone != tz_norm:
                raise ValueError(
                    f"Instrument {instrument.canonical_symbol} has confirmed market metadata "
                    f"{instrument.currency}/{instrument.exchange_timezone}; "
                    f"cannot overwrite with {currency_norm}/{tz_norm}."
                )
            return instrument

        now = _utc_now()
        self._conn.execute(
            """
            UPDATE market_instruments SET
                currency = ?,
                exchange_timezone = ?,
                market_metadata_confirmed = TRUE,
                updated_at = ?
            WHERE instrument_id = ?
            """,
            [currency_norm, tz_norm, now, instrument_id],
        )
        refreshed = self.get_instrument(instrument_id)
        if refreshed is None:
            raise RuntimeError(f"Failed to refresh instrument {instrument_id}")
        return refreshed

    def _require_instrument_exists(self, instrument_id: str) -> None:
        """Application-level FK: child rows must reference an existing instrument."""
        row = self._conn.execute(
            "SELECT 1 FROM market_instruments WHERE instrument_id = ? LIMIT 1",
            [instrument_id],
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Referential integrity violation: instrument_id {instrument_id!r} "
                "does not exist in market_instruments."
            )

    def count_active_common_tickers_for_issuer(self, issuer_cik: str) -> int:
        """Count distinct equity securities linked to an issuer CIK.

        The securities table has no share-class or active flag in v0.3a, so any
        multiple listed tickers for one issuer is treated as potential
        multi-class ambiguity (conservative).
        """
        return self.count_securities_for_issuer(issuer_cik)

    def count_securities_for_issuer(self, issuer_cik: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM securities WHERE cik = ?",
            [issuer_cik],
        ).fetchone()
        return int(row[0]) if row else 0

    def upsert_symbol_mapping(self, mapping: MarketSymbolMapping) -> MarketSymbolMapping:
        """Insert or update a provider mapping, allowing historical symbol reuse.

        Uniqueness is ``mapping_id`` (derived from provider, symbol, instrument,
        and valid_from). Overlapping primary mappings for the same
        instrument/provider (including same-instrument rows) are rejected.
        A provider_symbol may map to different instruments only in
        non-overlapping validity windows.
        """
        now = _utc_now()
        mapping_id = mapping.mapping_id or make_mapping_id(
            mapping.provider.value,
            mapping.provider_symbol,
            mapping.instrument_id,
            valid_from=mapping.valid_from,
        )
        self._require_instrument_exists(mapping.instrument_id)
        self._assert_mapping_invariants(mapping, mapping_id=mapping_id)
        self._conn.execute(
            """
            INSERT INTO market_symbol_mappings (
                mapping_id, instrument_id, provider, provider_symbol, valid_from, valid_to,
                is_primary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (mapping_id) DO UPDATE SET
                instrument_id = excluded.instrument_id,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                is_primary = excluded.is_primary,
                updated_at = excluded.updated_at
            """,
            [
                mapping_id,
                mapping.instrument_id,
                mapping.provider.value,
                mapping.provider_symbol,
                mapping.valid_from,
                mapping.valid_to,
                mapping.is_primary,
                now,
                now,
            ],
        )
        return mapping.model_copy(update={"mapping_id": mapping_id, "updated_at": now})

    def list_primary_mappings(
        self,
        instrument_id: str,
        provider: MarketDataProviderName,
    ) -> list[MarketSymbolMapping]:
        rows = self._conn.execute(
            """
            SELECT mapping_id, instrument_id, provider, provider_symbol, valid_from, valid_to,
                   is_primary, created_at, updated_at
            FROM market_symbol_mappings
            WHERE instrument_id = ?
              AND provider = ?
              AND is_primary = TRUE
            ORDER BY valid_from ASC NULLS FIRST, provider_symbol
            """,
            [instrument_id, provider.value],
        ).fetchall()
        return [_row_to_mapping(row) for row in rows]

    def mappings_covering_range(
        self,
        instrument_id: str,
        provider: MarketDataProviderName,
        *,
        start_date: date,
        end_date: date,
    ) -> list[MarketSymbolMapping]:
        """Return primary mappings whose validity overlaps ``[start_date, end_date]``."""
        out: list[MarketSymbolMapping] = []
        for mapping in self.list_primary_mappings(instrument_id, provider):
            if _intervals_overlap(mapping.valid_from, mapping.valid_to, start_date, end_date):
                out.append(mapping)
        return out

    def _assert_mapping_invariants(
        self,
        mapping: MarketSymbolMapping,
        *,
        mapping_id: str,
    ) -> None:
        if (
            mapping.valid_from is not None
            and mapping.valid_to is not None
            and mapping.valid_from > mapping.valid_to
        ):
            raise ValueError(
                f"Invalid mapping interval for {mapping.provider.value}/"
                f"{mapping.provider_symbol}: valid_from {mapping.valid_from} "
                f"is after valid_to {mapping.valid_to}."
            )

        # Provider-symbol reuse across instruments must be non-overlapping.
        rows = self._conn.execute(
            """
            SELECT mapping_id, instrument_id, provider_symbol, valid_from, valid_to, is_primary
            FROM market_symbol_mappings
            WHERE provider = ?
              AND provider_symbol = ?
              AND mapping_id <> ?
            """,
            [mapping.provider.value, mapping.provider_symbol, mapping_id],
        ).fetchall()
        for row in rows:
            if _intervals_overlap(mapping.valid_from, mapping.valid_to, row[3], row[4]):
                raise ValueError(
                    f"Overlapping provider mapping for {mapping.provider.value}/"
                    f"{mapping.provider_symbol} between instruments "
                    f"{mapping.instrument_id} and {row[1]}"
                )

        if not mapping.is_primary:
            return

        # Same instrument/provider: no overlapping primary mappings, including
        # two open-ended primary mappings or two different active symbols.
        peers = self._conn.execute(
            """
            SELECT mapping_id, provider_symbol, valid_from, valid_to
            FROM market_symbol_mappings
            WHERE instrument_id = ?
              AND provider = ?
              AND is_primary = TRUE
              AND mapping_id <> ?
            """,
            [mapping.instrument_id, mapping.provider.value, mapping_id],
        ).fetchall()
        for row in peers:
            if _intervals_overlap(mapping.valid_from, mapping.valid_to, row[2], row[3]):
                raise ValueError(
                    f"Overlapping primary mappings for instrument {mapping.instrument_id} "
                    f"provider {mapping.provider.value}: "
                    f"{mapping.provider_symbol!r} vs {row[1]!r}"
                )

    def get_active_symbol_mapping(
        self,
        instrument_id: str,
        provider: MarketDataProviderName,
        *,
        as_of: date | None = None,
    ) -> MarketSymbolMapping | None:
        params: list[object] = [instrument_id, provider.value]
        sql = """
            SELECT mapping_id, instrument_id, provider, provider_symbol, valid_from, valid_to,
                   is_primary, created_at, updated_at
            FROM market_symbol_mappings
            WHERE instrument_id = ?
              AND provider = ?
              AND is_primary = TRUE
        """
        if as_of is not None:
            sql += """
              AND (valid_from IS NULL OR valid_from <= ?)
              AND (valid_to IS NULL OR valid_to >= ?)
            """
            params.extend([as_of, as_of])
        else:
            sql += " AND valid_to IS NULL"
        sql += " ORDER BY valid_from DESC NULLS LAST, provider_symbol LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return _row_to_mapping(row) if row else None

    def upsert_price_bars(self, bars: list[DailyPriceBar]) -> UpsertCounts:
        """Persist bars. Caller may wrap in BEGIN/COMMIT for atomic mode writes."""
        counts = UpsertCounts()
        if not bars:
            return counts
        # Enforce once per batch; all bars in a mode share one instrument_id.
        instrument_ids = {bar.instrument_id for bar in bars}
        for instrument_id in instrument_ids:
            self._require_instrument_exists(instrument_id)
        now = _utc_now()
        for bar in bars:
            existing = self._conn.execute(
                """
                SELECT open, high, low, close, volume, currency, exchange_timezone,
                       available_at, fetched_at, source_metadata_json
                FROM daily_price_bars
                WHERE instrument_id = ?
                  AND provider = ?
                  AND trading_date = ?
                  AND adjustment_mode = ?
                """,
                [
                    bar.instrument_id,
                    bar.provider.value,
                    bar.trading_date,
                    bar.adjustment_mode.value,
                ],
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO daily_price_bars (
                        instrument_id, provider, trading_date, adjustment_mode,
                        open, high, low, close, volume, currency, exchange_timezone,
                        available_at, fetched_at, source_metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        bar.instrument_id,
                        bar.provider.value,
                        bar.trading_date,
                        bar.adjustment_mode.value,
                        str(bar.open),
                        str(bar.high),
                        str(bar.low),
                        str(bar.close),
                        bar.volume,
                        bar.currency,
                        bar.exchange_timezone,
                        bar.available_at,
                        bar.fetched_at,
                        bar.source_metadata_json,
                        now,
                        now,
                    ],
                )
                counts.inserted += 1
                continue

            same = (
                Decimal(str(existing[0])) == bar.open
                and Decimal(str(existing[1])) == bar.high
                and Decimal(str(existing[2])) == bar.low
                and Decimal(str(existing[3])) == bar.close
                and int(existing[4]) == bar.volume
                and str(existing[5]) == bar.currency
                and str(existing[6]) == bar.exchange_timezone
                and _as_utc(existing[7]) == _as_utc(bar.available_at)
                and (existing[9] or None) == (bar.source_metadata_json or None)
            )
            if same:
                counts.unchanged += 1
                continue

            self._conn.execute(
                """
                UPDATE daily_price_bars SET
                    open = ?, high = ?, low = ?, close = ?, volume = ?,
                    currency = ?, exchange_timezone = ?, available_at = ?,
                    fetched_at = ?, source_metadata_json = ?, updated_at = ?
                WHERE instrument_id = ?
                  AND provider = ?
                  AND trading_date = ?
                  AND adjustment_mode = ?
                """,
                [
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    bar.volume,
                    bar.currency,
                    bar.exchange_timezone,
                    bar.available_at,
                    bar.fetched_at,
                    bar.source_metadata_json,
                    now,
                    bar.instrument_id,
                    bar.provider.value,
                    bar.trading_date,
                    bar.adjustment_mode.value,
                ],
            )
            counts.updated += 1
        return counts

    def upsert_price_bars_atomic(self, bars: list[DailyPriceBar]) -> UpsertCounts:
        """Upsert bars in a single transaction; roll back the mode on failure."""
        self._conn.execute("BEGIN TRANSACTION")
        try:
            counts = self.upsert_price_bars(bars)
            self._conn.execute("COMMIT")
            return counts
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_price_bars(
        self,
        instrument_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        adjustment_mode: PriceAdjustmentMode = PriceAdjustmentMode.NONE,
        provider: MarketDataProviderName | None = None,
        limit: int | None = None,
        as_of: datetime | None = None,
    ) -> list[DailyPriceBar]:
        sql = """
            SELECT instrument_id, provider, trading_date, adjustment_mode,
                   open, high, low, close, volume, currency, exchange_timezone,
                   available_at, fetched_at, source_metadata_json
            FROM daily_price_bars
            WHERE instrument_id = ?
              AND adjustment_mode = ?
        """
        params: list[object] = [instrument_id, adjustment_mode.value]
        if provider is not None:
            sql += " AND provider = ?"
            params.append(provider.value)
        if start_date is not None:
            sql += " AND trading_date >= ?"
            params.append(start_date)
        if end_date is not None:
            sql += " AND trading_date <= ?"
            params.append(end_date)
        if as_of is not None:
            as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
            sql += " AND available_at <= ?"
            params.append(as_of_utc.astimezone(UTC))
        sql += " ORDER BY trading_date ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_bar(row) for row in rows]

    def get_price_bars_for_instruments(
        self,
        instrument_ids: Sequence[str],
        *,
        start_date: date,
        end_date: date,
        adjustment_mode: PriceAdjustmentMode,
        provider: MarketDataProviderName,
    ) -> list[DailyPriceBar]:
        """Load a date window of bars for many instruments in one query.

        Point-in-time ``available_at`` filtering stays with the caller so each
        decision can apply its own knowledge time.
        """
        ids = [item.strip() for item in instrument_ids if item.strip()]
        if not ids:
            return []
        placeholders = ", ".join("?" * len(ids))
        params: list[object] = [
            *ids,
            adjustment_mode.value,
            provider.value,
            start_date,
            end_date,
        ]
        rows = self._conn.execute(
            f"""
            SELECT instrument_id, provider, trading_date, adjustment_mode,
                   open, high, low, close, volume, currency, exchange_timezone,
                   available_at, fetched_at, source_metadata_json
            FROM daily_price_bars
            WHERE instrument_id IN ({placeholders})
              AND adjustment_mode = ?
              AND provider = ?
              AND trading_date >= ?
              AND trading_date <= ?
            ORDER BY instrument_id, trading_date ASC
            """,
            params,
        ).fetchall()
        return [_row_to_bar(row) for row in rows]

    def get_latest_price_on_or_before(
        self,
        instrument_id: str,
        on_or_before: date,
        *,
        adjustment_mode: PriceAdjustmentMode = PriceAdjustmentMode.NONE,
        provider: MarketDataProviderName | None = None,
        as_of: datetime | None = None,
    ) -> DailyPriceBar | None:
        sql = """
            SELECT instrument_id, provider, trading_date, adjustment_mode,
                   open, high, low, close, volume, currency, exchange_timezone,
                   available_at, fetched_at, source_metadata_json
            FROM daily_price_bars
            WHERE instrument_id = ?
              AND adjustment_mode = ?
              AND trading_date <= ?
        """
        params: list[object] = [instrument_id, adjustment_mode.value, on_or_before]
        if provider is not None:
            sql += " AND provider = ?"
            params.append(provider.value)
        if as_of is not None:
            as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
            sql += " AND available_at <= ?"
            params.append(as_of_utc.astimezone(UTC))
        sql += " ORDER BY trading_date DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return _row_to_bar(row) if row else None

    def get_price_as_of(
        self,
        instrument_id: str,
        as_of: datetime,
        *,
        adjustment_mode: PriceAdjustmentMode = PriceAdjustmentMode.NONE,
    ) -> DailyPriceBar | None:
        as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        row = self._conn.execute(
            """
            SELECT instrument_id, provider, trading_date, adjustment_mode,
                   open, high, low, close, volume, currency, exchange_timezone,
                   available_at, fetched_at, source_metadata_json
            FROM daily_price_bars
            WHERE instrument_id = ?
              AND adjustment_mode = ?
              AND available_at <= ?
            ORDER BY trading_date DESC, available_at DESC
            LIMIT 1
            """,
            [instrument_id, adjustment_mode.value, as_of_utc.astimezone(UTC)],
        ).fetchone()
        return _row_to_bar(row) if row else None

    def get_stored_date_range(
        self,
        instrument_id: str,
        *,
        adjustment_mode: PriceAdjustmentMode,
        provider: MarketDataProviderName | None = None,
    ) -> tuple[date | None, date | None]:
        sql = """
            SELECT MIN(trading_date), MAX(trading_date)
            FROM daily_price_bars
            WHERE instrument_id = ?
              AND adjustment_mode = ?
        """
        params: list[object] = [instrument_id, adjustment_mode.value]
        if provider is not None:
            sql += " AND provider = ?"
            params.append(provider.value)
        row = self._conn.execute(sql, params).fetchone()
        if not row:
            return None, None
        return row[0], row[1]

    def create_market_data_run(
        self,
        *,
        provider: MarketDataProviderName,
        instrument_id: str,
        requested_start_date: date,
        requested_end_date: date,
        adjustment_modes: list[PriceAdjustmentMode],
    ) -> str:
        run_id = str(uuid.uuid4())
        now = _utc_now()
        self._require_instrument_exists(instrument_id)
        self._conn.execute(
            """
            INSERT INTO market_data_runs (
                run_id, provider, instrument_id, requested_start_date, requested_end_date,
                adjustment_modes_json, started_at, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                provider.value,
                instrument_id,
                requested_start_date,
                requested_end_date,
                json.dumps([m.value for m in adjustment_modes]),
                now,
                MarketDataRunStatus.RUNNING.value,
                now,
            ],
        )
        return run_id

    def complete_market_data_run(
        self,
        run_id: str,
        *,
        status: MarketDataRunStatus,
        raw_row_count: int = 0,
        inserted_row_count: int = 0,
        updated_row_count: int = 0,
        unchanged_row_count: int = 0,
        rejected_row_count: int = 0,
        error_summary: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE market_data_runs SET
                completed_at = ?,
                status = ?,
                raw_row_count = ?,
                inserted_row_count = ?,
                updated_row_count = ?,
                unchanged_row_count = ?,
                rejected_row_count = ?,
                error_summary = ?
            WHERE run_id = ?
            """,
            [
                _utc_now(),
                status.value,
                raw_row_count,
                inserted_row_count,
                updated_row_count,
                unchanged_row_count,
                rejected_row_count,
                error_summary,
                run_id,
            ],
        )

    def upsert_stock_split_event(self, event: StockSplitEvent) -> str:
        """Insert or update a stock-split event. Returns inserted|updated|unchanged."""
        self._require_instrument_exists(event.instrument_id)
        existing = self._conn.execute(
            """
            SELECT from_factor, to_factor, share_multiplier, source_ratio, description,
                   available_at, retrieved_at, source_metadata_json, provider_symbol
            FROM stock_split_events
            WHERE event_id = ?
            """,
            [event.event_id],
        ).fetchone()
        now = _utc_now()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO stock_split_events (
                    event_id, instrument_id, provider, provider_symbol, effective_date,
                    from_factor, to_factor, share_multiplier, source_ratio, description,
                    available_at, retrieved_at, source_metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    event.event_id,
                    event.instrument_id,
                    event.provider.value,
                    event.provider_symbol,
                    event.effective_date,
                    str(event.from_factor),
                    str(event.to_factor),
                    str(event.share_multiplier),
                    str(event.source_ratio) if event.source_ratio is not None else None,
                    event.description,
                    event.available_at,
                    event.retrieved_at,
                    event.source_metadata_json,
                    now,
                    now,
                ],
            )
            return "inserted"

        same = (
            Decimal(str(existing[0])) == event.from_factor
            and Decimal(str(existing[1])) == event.to_factor
            and Decimal(str(existing[2])) == event.share_multiplier
            and (
                (existing[3] is None and event.source_ratio is None)
                or (
                    existing[3] is not None
                    and event.source_ratio is not None
                    and Decimal(str(existing[3])) == event.source_ratio
                )
            )
            and existing[4] == event.description
            and _as_utc(existing[5]) == event.available_at.astimezone(UTC)
            and str(existing[8]) == event.provider_symbol
            and existing[7] == event.source_metadata_json
        )
        if same:
            return "unchanged"

        self._conn.execute(
            """
            UPDATE stock_split_events SET
                provider_symbol = ?,
                from_factor = ?,
                to_factor = ?,
                share_multiplier = ?,
                source_ratio = ?,
                description = ?,
                available_at = ?,
                retrieved_at = ?,
                source_metadata_json = ?,
                updated_at = ?
            WHERE event_id = ?
            """,
            [
                event.provider_symbol,
                str(event.from_factor),
                str(event.to_factor),
                str(event.share_multiplier),
                str(event.source_ratio) if event.source_ratio is not None else None,
                event.description,
                event.available_at,
                event.retrieved_at,
                event.source_metadata_json,
                now,
                event.event_id,
            ],
        )
        return "updated"

    def upsert_stock_split_coverage(self, coverage: StockSplitCoverage) -> str:
        """Insert or update a coverage row. Returns inserted|updated|unchanged."""
        self._require_instrument_exists(coverage.instrument_id)
        existing = self._conn.execute(
            """
            SELECT status, completeness_basis, limitations_json, retrieved_at,
                   source_metadata_json, provider_symbol
            FROM stock_split_coverage
            WHERE coverage_id = ?
            """,
            [coverage.coverage_id],
        ).fetchone()
        now = _utc_now()
        limitations_json = json.dumps(list(coverage.limitations))
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO stock_split_coverage (
                    coverage_id, instrument_id, provider, provider_symbol,
                    coverage_start, coverage_end, status, completeness_basis,
                    limitations_json, retrieved_at, source_metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    coverage.coverage_id,
                    coverage.instrument_id,
                    coverage.provider.value,
                    coverage.provider_symbol,
                    coverage.coverage_start,
                    coverage.coverage_end,
                    coverage.status.value,
                    coverage.completeness_basis,
                    limitations_json,
                    coverage.retrieved_at,
                    coverage.source_metadata_json,
                    now,
                    now,
                ],
            )
            return "inserted"

        same = (
            str(existing[0]) == coverage.status.value
            and str(existing[1]) == coverage.completeness_basis
            and existing[2] == limitations_json
            and str(existing[4]) == coverage.source_metadata_json
            and str(existing[5]) == coverage.provider_symbol
        )
        if same:
            return "unchanged"

        self._conn.execute(
            """
            UPDATE stock_split_coverage SET
                provider_symbol = ?,
                status = ?,
                completeness_basis = ?,
                limitations_json = ?,
                retrieved_at = ?,
                source_metadata_json = ?,
                updated_at = ?
            WHERE coverage_id = ?
            """,
            [
                coverage.provider_symbol,
                coverage.status.value,
                coverage.completeness_basis,
                limitations_json,
                coverage.retrieved_at,
                coverage.source_metadata_json,
                now,
                coverage.coverage_id,
            ],
        )
        return "updated"

    def get_verified_split_coverage(
        self,
        instrument_id: str,
        *,
        shares_fact_date: date,
        price_date: date,
        provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA,
    ) -> StockSplitCoverage | None:
        """Return the best verified coverage fully containing (fact_date, price_date]."""
        if shares_fact_date >= price_date:
            return None
        first_event_day = shares_fact_date + timedelta(days=1)
        rows = self._conn.execute(
            """
            SELECT coverage_id, instrument_id, provider, provider_symbol,
                   coverage_start, coverage_end, status, completeness_basis,
                   limitations_json, retrieved_at, source_metadata_json
            FROM stock_split_coverage
            WHERE instrument_id = ?
              AND provider = ?
              AND status = ?
              AND coverage_start <= ?
              AND coverage_end >= ?
            ORDER BY coverage_start ASC, coverage_end ASC, coverage_id ASC
            """,
            [
                instrument_id,
                provider.value,
                SplitCoverageStatus.VERIFIED.value,
                first_event_day,
                price_date,
            ],
        ).fetchall()
        if not rows:
            # Surface non-verified rows for reason classification when present.
            fallback = self._conn.execute(
                """
                SELECT coverage_id, instrument_id, provider, provider_symbol,
                       coverage_start, coverage_end, status, completeness_basis,
                       limitations_json, retrieved_at, source_metadata_json
                FROM stock_split_coverage
                WHERE instrument_id = ?
                  AND provider = ?
                ORDER BY retrieved_at DESC, coverage_id ASC
                LIMIT 1
                """,
                [instrument_id, provider.value],
            ).fetchone()
            return _row_to_split_coverage(fallback) if fallback else None
        return _row_to_split_coverage(rows[0])

    def list_stock_split_events(
        self,
        instrument_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA,
        as_of: datetime | None = None,
    ) -> list[StockSplitEvent]:
        sql = """
            SELECT event_id, instrument_id, provider, provider_symbol, effective_date,
                   from_factor, to_factor, share_multiplier, source_ratio, description,
                   available_at, retrieved_at, source_metadata_json
            FROM stock_split_events
            WHERE instrument_id = ?
              AND provider = ?
        """
        params: list[object] = [instrument_id, provider.value]
        if start_date is not None:
            sql += " AND effective_date >= ?"
            params.append(start_date)
        if end_date is not None:
            sql += " AND effective_date <= ?"
            params.append(end_date)
        if as_of is not None:
            as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
            sql += " AND available_at <= ?"
            params.append(as_of_utc.astimezone(UTC))
        sql += " ORDER BY effective_date ASC, event_id ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_split_event(row) for row in rows]

    def build_stock_split_event(
        self,
        *,
        instrument_id: str,
        provider: MarketDataProviderName,
        provider_symbol: str,
        effective_date: date,
        from_factor: Decimal,
        to_factor: Decimal,
        share_multiplier: Decimal,
        source_ratio: Decimal | None,
        description: str | None,
        available_at: datetime,
        retrieved_at: datetime,
        source_metadata: dict[str, object] | None = None,
    ) -> StockSplitEvent:
        return StockSplitEvent(
            event_id=make_split_event_id(
                instrument_id,
                provider.value,
                effective_date,
                from_factor,
                to_factor,
            ),
            instrument_id=instrument_id,
            provider=provider,
            provider_symbol=provider_symbol,
            effective_date=effective_date,
            from_factor=from_factor,
            to_factor=to_factor,
            share_multiplier=share_multiplier,
            source_ratio=source_ratio,
            description=description,
            available_at=available_at,
            retrieved_at=retrieved_at,
            source_metadata_json=metadata_json(source_metadata),
        )

    def build_stock_split_coverage(
        self,
        *,
        instrument_id: str,
        provider: MarketDataProviderName,
        provider_symbol: str,
        coverage_start: date,
        coverage_end: date,
        status: SplitCoverageStatus,
        completeness_basis: str,
        limitations: tuple[str, ...] = (),
        retrieved_at: datetime,
        source_metadata: dict[str, object] | None = None,
    ) -> StockSplitCoverage:
        return StockSplitCoverage(
            coverage_id=make_split_coverage_id(
                instrument_id,
                provider.value,
                provider_symbol,
                coverage_start,
                coverage_end,
                completeness_basis,
            ),
            instrument_id=instrument_id,
            provider=provider,
            provider_symbol=provider_symbol,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            status=status,
            completeness_basis=completeness_basis,
            limitations=limitations,
            retrieved_at=retrieved_at,
            source_metadata_json=metadata_json(source_metadata),
        )


def _intervals_overlap(
    a_from: date | None,
    a_to: date | None,
    b_from: date | None,
    b_to: date | None,
) -> bool:
    """Inclusive interval overlap; None bounds mean open-ended."""
    start_a = a_from or date.min
    end_a = a_to or date.max
    start_b = b_from or date.min
    end_b = b_to or date.max
    return start_a <= end_b and start_b <= end_a


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"Expected datetime, got {type(value)!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _row_to_instrument(row: tuple[object, ...]) -> MarketInstrument:
    return MarketInstrument(
        instrument_id=str(row[0]),
        canonical_symbol=str(row[1]),
        asset_type=AssetType(str(row[2])),
        security_ticker=str(row[3]) if row[3] is not None else None,
        issuer_cik=str(row[4]) if row[4] is not None else None,
        exchange=str(row[5]) if row[5] is not None else None,
        mic_code=str(row[6]) if row[6] is not None else None,
        currency=str(row[7]),
        exchange_timezone=str(row[8]),
        market_metadata_confirmed=bool(row[9]),
        active=bool(row[10]),
        created_at=_as_utc(row[11]),
        updated_at=_as_utc(row[12]),
    )


def _row_to_mapping(row: tuple[object, ...]) -> MarketSymbolMapping:
    return MarketSymbolMapping(
        mapping_id=str(row[0]),
        instrument_id=str(row[1]),
        provider=MarketDataProviderName(str(row[2])),
        provider_symbol=str(row[3]),
        valid_from=row[4],
        valid_to=row[5],
        is_primary=bool(row[6]),
        created_at=_as_utc(row[7]),
        updated_at=_as_utc(row[8]),
    )


def _row_to_bar(row: tuple[object, ...]) -> DailyPriceBar:
    return DailyPriceBar(
        instrument_id=str(row[0]),
        provider=MarketDataProviderName(str(row[1])),
        trading_date=row[2],
        adjustment_mode=PriceAdjustmentMode(str(row[3])),
        open=Decimal(str(row[4])),
        high=Decimal(str(row[5])),
        low=Decimal(str(row[6])),
        close=Decimal(str(row[7])),
        volume=int(str(row[8])),
        currency=str(row[9]),
        exchange_timezone=str(row[10]),
        available_at=_as_utc(row[11]),
        fetched_at=_as_utc(row[12]),
        source_metadata_json=str(row[13]) if row[13] is not None else None,
    )


def _row_to_split_event(row: tuple[object, ...]) -> StockSplitEvent:
    return StockSplitEvent(
        event_id=str(row[0]),
        instrument_id=str(row[1]),
        provider=MarketDataProviderName(str(row[2])),
        provider_symbol=str(row[3]),
        effective_date=row[4],
        from_factor=Decimal(str(row[5])),
        to_factor=Decimal(str(row[6])),
        share_multiplier=Decimal(str(row[7])),
        source_ratio=Decimal(str(row[8])) if row[8] is not None else None,
        description=str(row[9]) if row[9] is not None else None,
        available_at=_as_utc(row[10]),
        retrieved_at=_as_utc(row[11]),
        source_metadata_json=str(row[12]) if row[12] is not None else None,
    )


def _row_to_split_coverage(row: tuple[object, ...]) -> StockSplitCoverage:
    limitations_raw = row[8]
    limitations: tuple[str, ...] = ()
    if limitations_raw is not None:
        parsed = json.loads(str(limitations_raw))
        if isinstance(parsed, list):
            limitations = tuple(str(item) for item in parsed)
    return StockSplitCoverage(
        coverage_id=str(row[0]),
        instrument_id=str(row[1]),
        provider=MarketDataProviderName(str(row[2])),
        provider_symbol=str(row[3]),
        coverage_start=row[4],
        coverage_end=row[5],
        status=SplitCoverageStatus(str(row[6])),
        completeness_basis=str(row[7]),
        limitations=limitations,
        retrieved_at=_as_utc(row[9]),
        source_metadata_json=str(row[10]) if row[10] is not None else None,
    )
