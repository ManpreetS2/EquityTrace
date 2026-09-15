"""Typed request, result, and status models for native portfolio backtests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from equitytrace.factors.models import FactorResult, RankedFactorResult
from equitytrace.market.models import MarketDataProviderName, PriceAdjustmentMode

WEIGHT_TOLERANCE = 1e-12
REQUIRED_RETURNS = 252
LOOKBACK_CALENDAR_DAYS = 550
ADJUSTED_HISTORY_WARNING = "provider_adjusted_history_not_vintage_pit"
SURVIVORSHIP_WARNING = "survivorship_bias_possible"
MIXED_PERIODS_WARNING = "mixed_fiscal_periods"

STANDING_WARNINGS = (ADJUSTED_HISTORY_WARNING, SURVIVORSHIP_WARNING)


class PortfolioRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class RebalanceStatus(StrEnum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class PortfolioSchedule(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    EXPLICIT = "explicit"


class PortfolioBaseline(StrEnum):
    EQUAL_WEIGHT = "equal_weight"
    INVERSE_VOL = "inverse_vol"


class BacktestRequest(BaseModel):
    """Immutable configuration for one research backtest."""

    model_config = ConfigDict(frozen=True)

    tickers: tuple[str, ...]
    factor: str
    top_n: int
    baseline: PortfolioBaseline
    start_date: date
    end_date: date
    schedule: PortfolioSchedule
    calendar_symbol: str = "SPY"
    benchmark_symbol: str | None = "SPY"
    cost_bps: float = 10.0
    initial_nav: float = 1.0
    explicit_dates: tuple[date, ...] = ()
    provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA
    adjustment_mode: PriceAdjustmentMode = PriceAdjustmentMode.ALL

    @field_validator("tickers")
    @classmethod
    def _tickers_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not any(item.strip() for item in value):
            raise ValueError("Provide at least one ticker.")
        return value

    @field_validator("top_n")
    @classmethod
    def _top_n_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("top_n must be >= 1.")
        return value

    @field_validator("cost_bps")
    @classmethod
    def _cost_bps_non_negative(cls, value: float) -> float:
        if not isfinite(value) or value < 0:
            raise ValueError("cost_bps must be a finite number >= 0.")
        return float(value)

    @field_validator("initial_nav")
    @classmethod
    def _initial_nav_positive(cls, value: float) -> float:
        if not isfinite(value) or value <= 0:
            raise ValueError("initial_nav must be a finite number > 0.")
        return float(value)

    @field_validator("calendar_symbol", "benchmark_symbol", mode="before")
    @classmethod
    def _upper_optional_symbol(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().upper()
            return text or None
        return value

    @model_validator(mode="after")
    def _dates_and_schedule(self) -> BacktestRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date.")
        if self.schedule is PortfolioSchedule.EXPLICIT and not self.explicit_dates:
            raise ValueError("explicit schedule requires explicit_dates.")
        return self

    @property
    def cost_rate(self) -> float:
        return self.cost_bps / 10_000.0


@dataclass
class PortfolioState:
    """Mutable engine holdings. Not a request/config object."""

    nav: float
    cash_weight: float
    asset_weights: dict[str, float] = field(default_factory=dict)
    last_valuation_session: date | None = None


class WeightTransition(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    instrument_id: str
    current_weight_before: float
    target_weight_after: float
    delta_weight: float
    gross_notional: float
    allocated_cost: float
    signal_value: float | None = None
    signal_rank: int | None = None
    signal_period: str | None = None
    signal_provenance_json: str | None = None


class RebalanceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_at: datetime
    target_effective_at: datetime
    status: RebalanceStatus
    reason: str | None = None
    pre_cost_nav: float
    post_cost_nav: float
    gross_turnover: float
    cost_amount: float
    transitions: tuple[WeightTransition, ...] = ()


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    valuation_at: datetime
    nav: float
    cash_weight: float
    drawdown: float
    benchmark_nav: float | None = None


class PerformanceMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_return: float | None = None
    annualized_return: float | None = None
    annualized_volatility: float | None = None
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None
    gross_turnover: float = 0.0
    transaction_cost_total: float = 0.0
    successful_rebalance_count: int = 0
    interval_count: int = 0


class LeakageAuditResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class SignalSelection(BaseModel):
    model_config = ConfigDict(frozen=True)

    ranked: tuple[RankedFactorResult, ...]
    mixed_fiscal_periods: bool = False
    evidence_available_at: dict[str, datetime] = Field(default_factory=dict)
    price_evidence_available_at: tuple[datetime, ...] = ()
    price_evidence_dates: tuple[date, ...] = ()


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: PortfolioRunStatus
    request: BacktestRequest
    failure_reason: str | None = None
    warnings: tuple[str, ...] = ()
    created_at: datetime
    completed_at: datetime | None = None
    universe: tuple[str, ...] = ()
    final_nav: float | None = None
    metrics: PerformanceMetrics | None = None
    rebalances: tuple[RebalanceRecord, ...] = ()
    equity: tuple[EquityPoint, ...] = ()
    audit: LeakageAuditResult | None = None
    package_version: str
    extra: dict[str, Any] = Field(default_factory=dict)


class FormedTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    weights: dict[str, float]
    cash_weight: float
    selection: SignalSelection
    factor_results: tuple[FactorResult, ...] = ()
