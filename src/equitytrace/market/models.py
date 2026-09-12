"""Market-data domain models for EquityTrace v0.3a."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from equitytrace.models import utc_now


class PriceAdjustmentMode(StrEnum):
    """How OHLC values relate to corporate actions."""

    NONE = "none"
    ALL = "all"
    # Reserved for later without storage redesign:
    SPLITS = "splits"
    DIVIDENDS = "dividends"


class AssetType(StrEnum):
    """Instrument asset classification."""

    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"


class MarketDataProviderName(StrEnum):
    """Supported market-data providers."""

    TWELVE_DATA = "twelve_data"


class MarketDataRunStatus(StrEnum):
    """Lifecycle status for a market-data ingestion run."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class MarketInstrument(BaseModel):
    """A priceable market instrument (equity, ETF, or index benchmark)."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    canonical_symbol: str
    asset_type: AssetType
    security_ticker: str | None = None
    issuer_cik: str | None = None
    exchange: str | None = None
    mic_code: str | None = None
    currency: str = "USD"
    exchange_timezone: str = "America/New_York"
    # False until a live provider response confirms currency/timezone.
    # Creation defaults (USD / America/New_York) are placeholders until confirmed.
    market_metadata_confirmed: bool = False
    active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("canonical_symbol", "security_ticker", mode="before")
    @classmethod
    def _upper_symbol(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper() or None
        return value


class MarketSymbolMapping(BaseModel):
    """Provider-specific symbol mapping for an instrument."""

    model_config = ConfigDict(frozen=True)

    mapping_id: str | None = None
    instrument_id: str
    provider: MarketDataProviderName
    provider_symbol: str
    valid_from: date | None = None
    valid_to: date | None = None
    is_primary: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("provider_symbol", mode="before")
    @classmethod
    def _strip_symbol(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class DailyPriceBar(BaseModel):
    """One daily OHLCV bar for a specific adjustment mode."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    provider: MarketDataProviderName
    trading_date: date
    adjustment_mode: PriceAdjustmentMode
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    currency: str = "USD"
    exchange_timezone: str = "America/New_York"
    available_at: datetime
    fetched_at: datetime
    source_metadata_json: str | None = None

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def _to_decimal(cls, value: object) -> object:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, float, str)):
            return Decimal(str(value))
        return value


class ProviderBar(BaseModel):
    """Normalized provider bar prior to availability stamping."""

    model_config = ConfigDict(frozen=True)

    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    currency: str = "USD"
    exchange_timezone: str = "America/New_York"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DailyBarsResponse(BaseModel):
    """Provider response containing normalized daily bars."""

    model_config = ConfigDict(frozen=True)

    provider: MarketDataProviderName
    provider_symbol: str
    adjustment_mode: PriceAdjustmentMode
    currency: str
    exchange_timezone: str
    bars: tuple[ProviderBar, ...] = ()
    meta: dict[str, Any] = Field(default_factory=dict)
    malformed_row_count: int = 0
    duplicate_date_count: int = 0
    duplicate_row_count: int = 0
    conflicting_duplicate_dates: tuple[date, ...] = ()
    incomplete: bool = False


class MarketDataIngestionResult(BaseModel):
    """Typed summary of a market-data ingestion run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    provider: MarketDataProviderName
    instrument_id: str
    canonical_symbol: str
    provider_symbol: str
    requested_start_date: date
    requested_end_date: date
    adjustment_modes: tuple[PriceAdjustmentMode, ...]
    status: MarketDataRunStatus
    raw_row_count: int = 0
    inserted_row_count: int = 0
    updated_row_count: int = 0
    unchanged_row_count: int = 0
    rejected_row_count: int = 0
    stored_start_date: date | None = None
    stored_end_date: date | None = None
    error_summary: str | None = None
    warnings: tuple[str, ...] = ()


class SharesOutstandingResult(BaseModel):
    """Point-in-time shares-outstanding selection from SEC facts."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    shares: Decimal | None
    fact_date: date | None = None
    available_at: datetime | None = None
    accession_number: str | None = None
    form: str | None = None
    concept: str | None = None
    taxonomy: str | None = None
    unit: str | None = None
    warnings: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.shares is not None and self.shares > 0


class MarketCapResult(BaseModel):
    """Historically safe market capitalization with full provenance."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    market_date_requested: date
    price_date_used: date | None = None
    knowledge_time: datetime | None = None
    raw_close: Decimal | None = None
    currency: str | None = None
    shares_outstanding: Decimal | None = None
    shares_fact_date: date | None = None
    shares_available_at: datetime | None = None
    market_cap: Decimal | None = None
    price_provider: MarketDataProviderName | None = None
    price_adjustment_mode: PriceAdjustmentMode | None = None
    price_fetched_at: datetime | None = None
    sec_concept: str | None = None
    sec_accession: str | None = None
    sec_form: str | None = None
    warnings: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.market_cap is not None


class MarketAnalyticsResult(BaseModel):
    """Inspectable result for a stored-price market-window metric."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    metric: str
    value: float | None
    as_of: datetime
    valid: bool = False
    unavailable_reason: str | None = None
    window_start: date | None = None
    window_end: date | None = None
    observation_count: int = 0
    provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA
    adjustment_mode: PriceAdjustmentMode = PriceAdjustmentMode.ALL
    benchmark: str | None = None
    warnings: tuple[str, ...] = ()
    first_trading_date: date | None = None
    last_trading_date: date | None = None
    latest_available_at: datetime | None = None
    fetched_at_min: datetime | None = None
    fetched_at_max: datetime | None = None
    ranking_direction: Literal["higher_is_better", "lower_is_better"] | None = None


class RankedMarketMetricResult(BaseModel):
    """A market metric result with cross-sectional rank metadata."""

    model_config = ConfigDict(frozen=True)

    result: MarketAnalyticsResult
    rank: int | None = None
    percentile: float | None = None


class MarketMetricRankingResult(BaseModel):
    """Cross-sectional ranking of a market-window metric."""

    model_config = ConfigDict(frozen=True)

    metric: str
    as_of: datetime
    ranking_direction: Literal["higher_is_better", "lower_is_better"]
    rows: tuple[RankedMarketMetricResult, ...]
    valid_count: int
    excluded: tuple[tuple[str, str], ...] = ()
    benchmark: str | None = None


class MarketCapSeriesPoint(BaseModel):
    """One observation in a market-cap series."""

    model_config = ConfigDict(frozen=True)

    as_of_date: date
    result: MarketCapResult


def make_instrument_id(canonical_symbol: str) -> str:
    """Deterministic instrument identifier from the canonical symbol.

    v0.3a derives identity from the current display ticker. Historical ticker
    reuse across issuers is a documented limitation until a later migration
    separates display symbols from durable instrument identity.
    """
    symbol = canonical_symbol.strip().upper()
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:16]
    return f"mkt_{digest}"


def make_mapping_id(
    provider: str,
    provider_symbol: str,
    instrument_id: str,
    *,
    valid_from: date | None = None,
) -> str:
    """Deterministic mapping identifier supporting historical reuse."""
    start = valid_from.isoformat() if valid_from is not None else "open"
    material = f"{provider}|{provider_symbol}|{instrument_id}|{start}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"map_{digest}"


def metadata_json(payload: dict[str, Any] | None) -> str | None:
    """Serialize optional metadata without secrets."""
    if not payload:
        return None
    cleaned = {k: v for k, v in payload.items() if "key" not in k.lower()}
    return json.dumps(cleaned, sort_keys=True, default=str)
