"""Explicit verified split-coverage fixtures for offline market-cap tests.

Callers that model a known no-split (or known-split) interval must seed coverage
with a documented scope. An empty event table never implies no splits occurred.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from equitytrace.market.availability import modeled_split_available_at
from equitytrace.market.models import (
    MarketDataProviderName,
    SplitCoverageStatus,
)
from equitytrace.models import utc_now
from equitytrace.repositories.market import MarketRepository

# Documented synthetic completeness basis for offline fixtures only.
FIXTURE_COVERAGE_BASIS = "synthetic_fixture_verified_no_split_interval"


def seed_verified_split_coverage(
    conn: object,
    *,
    instrument_id: str,
    coverage_start: date,
    coverage_end: date,
    provider_symbol: str,
    provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA,
    completeness_basis: str = FIXTURE_COVERAGE_BASIS,
    status: SplitCoverageStatus = SplitCoverageStatus.VERIFIED,
    limitations: tuple[str, ...] = (
        "synthetic_fixture_coverage",
        "not_live_provider_history",
    ),
) -> str:
    """Persist an explicit coverage interval for the given instrument."""
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    coverage = repo.build_stock_split_coverage(
        instrument_id=instrument_id,
        provider=provider,
        provider_symbol=provider_symbol,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        status=status,
        completeness_basis=completeness_basis,
        limitations=limitations,
        retrieved_at=utc_now(),
        source_metadata={"fixture": True},
    )
    repo.upsert_stock_split_coverage(coverage)
    return coverage.coverage_id


def seed_split_event(
    conn: object,
    *,
    instrument_id: str,
    provider_symbol: str,
    effective_date: date,
    from_factor: Decimal | int | str,
    to_factor: Decimal | int | str,
    description: str | None = None,
    source_ratio: Decimal | int | str | None = None,
    provider: MarketDataProviderName = MarketDataProviderName.TWELVE_DATA,
    available_at: datetime | None = None,
    retrieved_at: datetime | None = None,
) -> str:
    """Persist one synthetic stock-split event with modeled eligibility."""
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    from_dec = Decimal(str(from_factor))
    to_dec = Decimal(str(to_factor))
    multiplier = from_dec / to_dec
    event = repo.build_stock_split_event(
        instrument_id=instrument_id,
        provider=provider,
        provider_symbol=provider_symbol,
        effective_date=effective_date,
        from_factor=from_dec,
        to_factor=to_dec,
        share_multiplier=multiplier,
        source_ratio=Decimal(str(source_ratio)) if source_ratio is not None else None,
        description=description,
        available_at=available_at or modeled_split_available_at(effective_date),
        retrieved_at=retrieved_at or datetime(2024, 1, 1, tzinfo=UTC),
        source_metadata={"fixture": True},
    )
    repo.upsert_stock_split_event(event)
    return event.event_id
