"""Frozen ResearchStrategy model and economic fingerprint helpers.

Identity contract (Milestone 1):
- Human display fields (name, description) live in the strategy file only.
- Economic identity is the canonical BacktestRequest fingerprint.
- Persisted runs store BacktestRequest via request_json; fingerprints are
  recomputed from PortfolioRepository.get_run(...).request without schema changes.
- name/description are intentionally NOT persisted and are not promised by
  ``research show`` when the original strategy file is absent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from math import isfinite

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from equitytrace.factors.registry import UnknownFactorError, normalize_factor_name
from equitytrace.market.models import MarketDataProviderName, PriceAdjustmentMode
from equitytrace.portfolio.models import BacktestRequest, PortfolioBaseline, PortfolioSchedule


class ResearchStrategy(BaseModel):
    """Transparent, deterministic research strategy configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    tickers: tuple[str, ...]
    factor: str
    top_n: int
    baseline: PortfolioBaseline
    start_date: date
    end_date: date
    schedule: PortfolioSchedule
    description: str | None = None
    explicit_dates: tuple[date, ...] = ()
    calendar_symbol: str = "SPY"
    benchmark_symbol: str | None = "SPY"
    cost_bps: float = 10.0
    initial_nav: float = 1.0
    provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA
    adjustment_mode: PriceAdjustmentMode = PriceAdjustmentMode.ALL

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("name must be non-empty.")
        return text

    @field_validator("description", mode="before")
    @classmethod
    def _description_optional(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return value

    @field_validator("tickers", mode="before")
    @classmethod
    def _canonicalize_tickers(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("tickers must be a list or tuple of strings.")
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("each ticker must be a string.")
            symbol = raw.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            cleaned.append(symbol)
        if not cleaned:
            raise ValueError("Provide at least one ticker.")
        return tuple(sorted(cleaned))

    @field_validator("factor")
    @classmethod
    def _canonicalize_factor(cls, value: str) -> str:
        try:
            return normalize_factor_name(value)
        except UnknownFactorError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("top_n")
    @classmethod
    def _top_n_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("top_n must be >= 1.")
        return value

    @field_validator("calendar_symbol")
    @classmethod
    def _calendar_symbol(cls, value: str) -> str:
        text = value.strip().upper()
        if not text:
            raise ValueError("calendar_symbol must be non-empty.")
        return text

    @field_validator("benchmark_symbol", mode="before")
    @classmethod
    def _benchmark_symbol(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().upper()
            return text or None
        return value

    @field_validator("explicit_dates", mode="before")
    @classmethod
    def _canonicalize_explicit_dates(cls, value: object) -> tuple[date, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("explicit_dates must be a list or tuple of dates.")
        parsed: list[date] = []
        seen: set[date] = set()
        for item in value:
            if isinstance(item, date) and not isinstance(item, type(None)):
                day = item
            elif isinstance(item, str):
                day = date.fromisoformat(item.strip())
            else:
                raise ValueError("explicit_dates entries must be ISO dates.")
            if day in seen:
                continue
            seen.add(day)
            parsed.append(day)
        return tuple(sorted(parsed))

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

    @field_validator("adjustment_mode")
    @classmethod
    def _adjusted_only(cls, value: PriceAdjustmentMode) -> PriceAdjustmentMode:
        if value is not PriceAdjustmentMode.ALL:
            raise ValueError("v1.0 research strategies require adjustment_mode=all.")
        return value

    @model_validator(mode="after")
    def _cross_field(self) -> ResearchStrategy:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date.")
        if self.top_n > len(self.tickers):
            raise ValueError("top_n must be <= number of unique universe tickers.")
        if self.schedule is PortfolioSchedule.EXPLICIT:
            if not self.explicit_dates:
                raise ValueError("explicit schedule requires explicit_dates.")
            for day in self.explicit_dates:
                if day < self.start_date or day > self.end_date:
                    raise ValueError(
                        f"explicit date {day.isoformat()} is outside "
                        f"[{self.start_date.isoformat()}, {self.end_date.isoformat()}]."
                    )
        elif self.explicit_dates:
            raise ValueError("explicit_dates must be empty unless schedule is explicit.")
        return self

    def to_backtest_request(self) -> BacktestRequest:
        """Project validated strategy config onto the existing BacktestRequest."""
        return BacktestRequest(
            tickers=self.tickers,
            factor=self.factor,
            top_n=self.top_n,
            baseline=self.baseline,
            start_date=self.start_date,
            end_date=self.end_date,
            schedule=self.schedule,
            calendar_symbol=self.calendar_symbol,
            benchmark_symbol=self.benchmark_symbol,
            cost_bps=self.cost_bps,
            initial_nav=self.initial_nav,
            explicit_dates=self.explicit_dates,
            provider=self.provider,
            adjustment_mode=PriceAdjustmentMode.ALL,
        )


def fingerprint_request(request: BacktestRequest) -> str:
    """SHA-256 of the economic BacktestRequest (canonical JSON).

    Excludes strategy display metadata (name/description), run_id, and timestamps.
    Safe to recompute from a persisted request loaded via PortfolioRepository.get_run.
    """
    payload = request.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fingerprint_strategy(strategy: ResearchStrategy) -> str:
    """Economic fingerprint equal to fingerprint_request(strategy.to_backtest_request())."""
    return fingerprint_request(strategy.to_backtest_request())
