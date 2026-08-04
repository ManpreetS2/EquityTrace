"""High-level factor calculation engine with optional DuckDB persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import duckdb

from equitytrace.factors.models import FactorResult, RankingResult
from equitytrace.factors.ranking import rank_factors
from equitytrace.factors.registry import get_factor, list_factors, normalize_factor_name
from equitytrace.financials.models import FinancialPeriod
from equitytrace.financials.periods import parse_period
from equitytrace.financials.service import FinancialsService
from equitytrace.repositories.factors import FactorRepository


class FactorEngine:
    """Calculate factors on demand and optionally materialize results."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._service = FinancialsService(conn)
        self._repo = FactorRepository(conn)

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
        result: FactorResult = factor_obj.calculate(
            ticker,
            as_of_utc,
            parsed,
            service=self._service,
            market_cap=market_cap,
        )
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
        """Rank tickers for a factor."""
        return rank_factors(
            tickers=tickers,
            factor=factor,
            as_of=_as_of(as_of),
            period=period,
            service=self._service,
            market_cap_by_ticker=market_cap_by_ticker,
        )


def _as_of(as_of: datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(UTC)
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=UTC)
    return as_of.astimezone(UTC)
