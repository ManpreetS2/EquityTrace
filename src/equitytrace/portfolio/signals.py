"""Latest-FY factor resolution per security/ticker and exact top-N selection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from equitytrace.factors.engine import FactorEngine
from equitytrace.factors.models import FactorResult, RankedFactorResult
from equitytrace.factors.ranking import rank_factor_results
from equitytrace.factors.registry import get_factor, normalize_factor_name
from equitytrace.financials.models import FinancialSnapshot
from equitytrace.financials.service import FinancialsError, FinancialsService
from equitytrace.portfolio.models import MIXED_PERIODS_WARNING, SignalSelection


def normalize_universe(tickers: tuple[str, ...] | list[str]) -> list[str]:
    """Uppercase, strip, and keep first-seen duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in tickers:
        symbol = raw.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def tickers_sharing_an_issuer(cik_by_symbol: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Group universe tickers that resolve to the same issuer CIK.

    Does not pick a share class. Callers should fail closed when the result
    is non-empty.
    """
    grouped: dict[str, list[str]] = {}
    for symbol, cik in cik_by_symbol.items():
        issuer = cik.strip()
        if not issuer:
            continue
        grouped.setdefault(issuer, []).append(symbol)
    return {cik: tuple(names) for cik, names in grouped.items() if len(names) > 1}


def resolve_latest_annual_factor(
    engine: FactorEngine,
    ticker: str,
    factor: str,
    decision_at: datetime,
) -> tuple[FactorResult | None, str | None, datetime | None]:
    """Calculate the factor on the latest FY known for this ticker at ``decision_at``.

    Fundamentals are issuer-level, but eligibility is resolved per security
    ticker. Older fiscal years are never substituted when the latest year is
    unusable.
    """
    periods = engine.financials.list_available_annual_periods(ticker, decision_at)
    if not periods:
        return None, "no_annual_period", None
    latest = periods[0]
    try:
        result = engine.calculate(
            ticker,
            factor,
            latest,
            as_of=decision_at,
            persist=False,
        )
    except FinancialsError as exc:
        return None, str(exc), None
    evidence = _snapshot_evidence_time(engine.financials, ticker, latest, decision_at)
    if not result.valid or result.value is None:
        return None, result.unavailable_reason or "factor_unavailable", evidence
    return result, None, evidence


def select_exact_top_n(
    results: list[FactorResult],
    *,
    factor: str,
    as_of: datetime,
    top_n: int,
    evidence_available_at: dict[str, datetime],
) -> SignalSelection | None:
    if len(results) < top_n:
        return None
    factor_name = normalize_factor_name(factor)
    factor_obj = get_factor(factor_name)
    ranking = rank_factor_results(
        results=results,
        excluded=[],
        factor=factor_name,
        period=results[0].period,
        as_of=as_of,
        ranking_direction=factor_obj.ranking_direction,
    )
    if ranking.valid_count < top_n:
        return None
    ranked = ranking.rows[:top_n]
    years = {row.result.period.fiscal_year for row in ranked}
    return SignalSelection(
        ranked=tuple(ranked),
        mixed_fiscal_periods=len(years) > 1,
        evidence_available_at=evidence_available_at,
    )


def selected_symbols(ranked: tuple[RankedFactorResult, ...]) -> list[str]:
    return [row.result.ticker for row in ranked]


def mixed_period_warning(selection: SignalSelection) -> str | None:
    if selection.mixed_fiscal_periods:
        return MIXED_PERIODS_WARNING
    return None


def _snapshot_evidence_time(
    financials: FinancialsService,
    ticker: str,
    period: object,
    as_of: datetime,
) -> datetime | None:
    try:
        snapshot = financials.get_snapshot(ticker, period, as_of=as_of)  # type: ignore[arg-type]
    except FinancialsError:
        return None
    if not isinstance(snapshot, FinancialSnapshot):
        return None
    times: list[datetime] = []
    for section in (
        snapshot.income_statement,
        snapshot.balance_sheet,
        snapshot.cash_flow,
        snapshot.derived,
    ):
        for value in section.values():
            if value.value is None or value.provenance.available_at is None:
                continue
            times.append(value.provenance.available_at)
    return max(times) if times else None
