"""Factor result models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from equitytrace.financials.models import FinancialPeriod
from equitytrace.market.models import MarketCapResult


class FactorResult(BaseModel):
    """Outcome of a single factor calculation."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    cik: str
    factor: str
    value: float | None
    as_of: datetime
    period: FinancialPeriod
    inputs: dict[str, float | None] = Field(default_factory=dict)
    source_filings: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    market_input: MarketCapResult | None = None
    valid: bool = False
    unavailable_reason: str | None = None
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"


class RankedFactorResult(BaseModel):
    """A factor result with cross-sectional rank metadata."""

    model_config = ConfigDict(frozen=True)

    result: FactorResult
    rank: int | None = None
    percentile: float | None = None


class RankingResult(BaseModel):
    """Cross-sectional ranking output."""

    model_config = ConfigDict(frozen=True)

    factor: str
    period: FinancialPeriod
    as_of: datetime
    ranking_direction: Literal["higher_is_better", "lower_is_better"]
    rows: tuple[RankedFactorResult, ...]
    valid_count: int
    excluded: tuple[tuple[str, str], ...] = ()
