"""High-level factor calculation engine with optional DuckDB persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import duckdb

from equitytrace.factors.models import FactorResult, RankingResult
from equitytrace.factors.ranking import rank_factor_results
from equitytrace.factors.registry import get_factor, list_factors, normalize_factor_name
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.periods import parse_period
from equitytrace.financials.service import FinancialsError, FinancialsService
from equitytrace.market.market_cap import MarketCapService
from equitytrace.market.models import MarketCapResult
from equitytrace.repositories.factors import FactorRepository

# Canonical statements are USD. A foreign-currency cap is not a usable multiple.
_CANONICAL_CURRENCY = "USD"
_MANUAL_MARKET_CAP_WARNING = "manual_market_cap_override"


class FactorEngine:
    """Calculate factors on demand and optionally materialize results."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._service = FinancialsService(conn)
        self._repo = FactorRepository(conn)
        self._market_cap = MarketCapService(conn)

    @property
    def financials(self) -> FinancialsService:
        return self._service

    def calculate(
        self,
        ticker: str,
        factor: str,
        period: str | FinancialPeriod,
        *,
        as_of: datetime | None = None,
        market_cap: float | None = None,
        persist: bool = True,
    ) -> FactorResult:
        """Calculate one factor; persistence is idempotent by natural key."""
        as_of_utc = _as_of(as_of)
        parsed = period if isinstance(period, FinancialPeriod) else parse_period(period)
        factor_name = normalize_factor_name(factor)
        factor_obj = get_factor(factor_name)
        resolved_cap, market_input = self._resolve_market_cap(
            ticker,
            as_of_utc,
            factor_obj=factor_obj,
            market_cap=market_cap,
        )
        result: FactorResult = factor_obj.calculate(
            ticker,
            as_of_utc,
            parsed,
            service=self._service,
            market_cap=resolved_cap,
        )
        if market_input is not None:
            result = _attach_market_input(result, market_input, resolved_cap)
        if persist:
            run_id = uuid4().hex
            self._repo.record_run(
                run_id=run_id,
                ticker=result.ticker,
                cik=result.cik,
                factor=factor_name,
                period=parsed,
                as_of=as_of_utc,
                status="success" if result.valid else "unavailable",
            )
            self._repo.upsert_value(run_id=run_id, result=result)
        return result

    def calculate_all(
        self,
        ticker: str,
        period: str | FinancialPeriod,
        *,
        as_of: datetime | None = None,
        market_cap: float | None = None,
        persist: bool = True,
    ) -> list[FactorResult]:
        """Calculate every registered factor for one ticker/period."""
        return [
            self.calculate(
                ticker,
                name,
                period,
                as_of=as_of,
                market_cap=market_cap,
                persist=persist,
            )
            for name in list_factors()
        ]

    def rank(
        self,
        tickers: list[str],
        factor: str,
        period: str | FinancialPeriod,
        *,
        as_of: datetime | None = None,
        market_cap_by_ticker: dict[str, float] | None = None,
    ) -> RankingResult:
        """Rank tickers using the same calculation path as ``calculate``."""
        as_of_utc = _as_of(as_of)
        parsed = period if isinstance(period, FinancialPeriod) else parse_period(period)
        factor_name = normalize_factor_name(factor)
        factor_obj = get_factor(factor_name)
        market_caps = market_cap_by_ticker or {}
        results: list[FactorResult] = []
        excluded: list[tuple[str, str]] = []
        for ticker in tickers:
            symbol = ticker.strip().upper()
            if not symbol:
                continue
            try:
                result = self.calculate(
                    symbol,
                    factor_name,
                    parsed,
                    as_of=as_of_utc,
                    market_cap=market_caps.get(symbol),
                    persist=False,
                )
            except FinancialsError as exc:
                excluded.append((symbol, str(exc)))
                continue
            if not result.valid or result.value is None:
                excluded.append((symbol, result.unavailable_reason or "invalid result"))
                continue
            results.append(result)
        return rank_factor_results(
            results=results,
            excluded=excluded,
            factor=factor_name,
            period=parsed,
            as_of=as_of_utc,
            ranking_direction=factor_obj.ranking_direction,
        )

    def _resolve_market_cap(
        self,
        ticker: str,
        as_of: datetime,
        *,
        factor_obj: object,
        market_cap: float | None,
    ) -> tuple[float | None, MarketCapResult | None]:
        if not getattr(factor_obj, "requires_market_cap", False):
            return market_cap, None
        symbol = ticker.strip().upper()
        if market_cap is not None:
            # Manual override is not stored market data; provenance stays empty.
            override = MarketCapResult(
                ticker=symbol,
                market_date_requested=as_of.date(),
                knowledge_time=as_of,
                market_cap=Decimal(str(market_cap)),
                warnings=(_MANUAL_MARKET_CAP_WARNING,),
            )
            return market_cap, override
        native = self._market_cap.get_market_cap(
            symbol,
            as_of.date(),
            as_of=as_of,
        )
        if native.is_available and native.market_cap is not None and not _currency_mismatch(native):
            return float(native.market_cap), native
        return None, native


def _currency_mismatch(market_input: MarketCapResult) -> bool:
    currency = (market_input.currency or "").strip().upper()
    return bool(currency) and currency != _CANONICAL_CURRENCY


def _attach_market_input(
    result: FactorResult,
    market_input: MarketCapResult,
    resolved_cap: float | None,
) -> FactorResult:
    extra = list(result.warnings)
    extra.extend(market_input.warnings)
    updates: dict[str, object] = {"market_input": market_input}
    # Native market-cap detail only when the factor actually lacked a usable cap.
    # Do not hide annual_period_required or other fundamental unavailable reasons.
    needed_cap = "market_cap_missing" in result.warnings
    if _currency_mismatch(market_input) and needed_cap:
        updates["valid"] = False
        updates["value"] = None
        updates["unavailable_reason"] = (
            "Market-cap currency incompatible with canonical USD financials"
        )
        extra.append("currency_mismatch")
    elif resolved_cap is None and needed_cap and market_input.unavailable_reason:
        updates["unavailable_reason"] = (
            f"Market capitalization unavailable ({market_input.unavailable_reason})"
        )
        extra.append(market_input.unavailable_reason)
    updates["warnings"] = tuple(dict.fromkeys(extra))
    return result.model_copy(update=updates)


def _as_of(as_of: datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(UTC)
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=UTC)
    return as_of.astimezone(UTC)
