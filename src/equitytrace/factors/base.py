"""Factor protocol and shared helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod, FinancialSnapshot
from equitytrace.financials.service import FinancialsService


class Factor(Protocol):
    """Reusable point-in-time factor interface."""

    name: str
    ranking_direction: Literal["higher_is_better", "lower_is_better"]

    def calculate(
        self,
        ticker: str,
        as_of: datetime,
        period: FinancialPeriod,
        *,
        service: FinancialsService,
        market_cap: float | None = None,
    ) -> FactorResult: ...


def snapshot_accessions(snapshot: FinancialSnapshot, *concepts: str) -> tuple[str, ...]:
    """Collect accession numbers for the given concepts, including derivation inputs."""
    found: list[str] = []
    for concept in concepts:
        value = snapshot.get_value(concept)
        if value is None:
            continue
        if value.provenance.accession_number:
            found.append(value.provenance.accession_number)
        found.extend(value.provenance.derivation_accessions)
    return tuple(dict.fromkeys(found))


def invalid_result(
    *,
    ticker: str,
    cik: str,
    factor: str,
    as_of: datetime,
    period: FinancialPeriod,
    reason: str,
    warnings: tuple[str, ...] = (),
    inputs: dict[str, float | None] | None = None,
    ranking_direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better",
    source_filings: tuple[str, ...] = (),
) -> FactorResult:
    """Build a structured unavailable / invalid factor result."""
    return FactorResult(
        ticker=ticker,
        cik=cik,
        factor=factor,
        value=None,
        as_of=as_of,
        period=period,
        inputs=inputs or {},
        source_filings=source_filings,
        warnings=warnings or ("missing_input",),
        valid=False,
        unavailable_reason=reason,
        ranking_direction=ranking_direction,
    )
