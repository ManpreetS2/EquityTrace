"""Stock-split ratio parsing and point-in-time share-basis reconciliation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from equitytrace.market.models import StockSplitCoverage, StockSplitEvent

# Retrospectively retrieved provider split history is not vendor-vintage PIT.
SPLIT_HISTORY_NON_VINTAGE_WARNING = "split_history_not_vendor_vintage_pit"
SPLIT_ELIGIBILITY_CONVENTION_WARNING = "split_available_at_modeled_eod_convention"

_DESCRIPTION_PATTERN = re.compile(
    r"(?P<left>\d+(?:\.\d+)?)\s*[- ]\s*for\s*[- ]\s*(?P<right>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class SplitRatioError(ValueError):
    """Malformed or inconsistent stock-split ratio inputs."""


@dataclass(frozen=True, slots=True)
class ParsedSplitRatio:
    """Exact share-count multiplier derived from provider from/to factors."""

    from_factor: Decimal
    to_factor: Decimal
    share_multiplier: Decimal
    source_ratio: Decimal | None
    description: str | None


@dataclass(frozen=True, slots=True)
class ShareBasisReconciliation:
    """Result of reconciling SEC shares to the raw-price share basis."""

    original_shares: Decimal
    reconciled_shares: Decimal
    share_multiplier: Decimal
    events: tuple[StockSplitEvent, ...]
    coverage: StockSplitCoverage | None
    warnings: tuple[str, ...]
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.unavailable_reason is None


def parse_split_ratio(
    *,
    from_factor: object,
    to_factor: object,
    source_ratio: object | None = None,
    description: str | None = None,
) -> ParsedSplitRatio:
    """Derive the share multiplier from Twelve Data from/to factors.

    Twelve Data's documented 4-for-1 example returns ``from_factor=4``,
    ``to_factor=1``, ``ratio=0.25``. The share-count multiplier is
    ``from_factor / to_factor`` (= 4), never the raw ``ratio`` field alone.
    """
    from_dec = _positive_decimal(from_factor, "from_factor")
    to_dec = _positive_decimal(to_factor, "to_factor")
    multiplier = from_dec / to_dec
    if multiplier <= 0:
        raise SplitRatioError("share_multiplier must be positive")

    ratio_dec: Decimal | None = None
    if source_ratio is not None and str(source_ratio).strip() != "":
        ratio_dec = _positive_decimal(source_ratio, "ratio")
        # Twelve Data documents ratio as the inverse share factor
        # (4-for-1 => ratio 0.25 = to_factor / from_factor). Never treat ratio as
        # the share multiplier by itself.
        expected_price_factor = to_dec / from_dec
        if not _nearly_equal(ratio_dec, expected_price_factor):
            raise SplitRatioError(
                "source ratio is inconsistent with from_factor/to_factor "
                f"(ratio={ratio_dec}, expected {expected_price_factor} = to/from)"
            )

    if description:
        match = _DESCRIPTION_PATTERN.search(description)
        if match is not None:
            left = Decimal(match.group("left"))
            right = Decimal(match.group("right"))
            if right <= 0 or left <= 0:
                raise SplitRatioError(f"invalid description factors in {description!r}")
            described = left / right
            if not _nearly_equal(described, multiplier):
                raise SplitRatioError(
                    "description factors disagree with from_factor/to_factor "
                    f"({description!r} => {described}, factors => {multiplier})"
                )

    return ParsedSplitRatio(
        from_factor=from_dec,
        to_factor=to_dec,
        share_multiplier=multiplier,
        source_ratio=ratio_dec,
        description=description,
    )


def coverage_covers_gap(
    coverage: StockSplitCoverage,
    *,
    shares_fact_date: date,
    price_date: date,
) -> bool:
    """Return True when verified coverage fully contains (fact_date, price_date]."""
    if shares_fact_date >= price_date:
        return True
    if coverage.status.value != "verified":
        return False
    interval_start = shares_fact_date + timedelta(days=1)
    return coverage.coverage_start <= interval_start and coverage.coverage_end >= price_date


def events_in_gap(
    events: list[StockSplitEvent],
    *,
    shares_fact_date: date,
    price_date: date,
    knowledge_time: datetime,
) -> tuple[tuple[StockSplitEvent, ...], str | None]:
    """Select PIT-eligible events in (fact_date, price_date]; detect conflicts.

    A stored effective split that is not yet eligible at ``knowledge_time`` is
    not treated as absent. Coverage alone cannot certify the pre-split share
    basis while such an event remains ineligible.
    """
    candidates = [
        event
        for event in sorted(events, key=lambda e: (e.effective_date, e.event_id))
        if shares_fact_date < event.effective_date <= price_date
    ]
    by_date: dict[date, StockSplitEvent] = {}
    for event in candidates:
        prior = by_date.get(event.effective_date)
        if prior is not None and prior.share_multiplier != event.share_multiplier:
            return (), "conflicting_split_events"
        if prior is None:
            by_date[event.effective_date] = event

    for event in by_date.values():
        if event.available_at > knowledge_time:
            return (), "split_event_unavailable_at_knowledge_time"

    selected = tuple(by_date[d] for d in sorted(by_date))
    return selected, None


def cumulative_share_multiplier(events: tuple[StockSplitEvent, ...]) -> Decimal:
    """Compound share multipliers in effective-date order."""
    total = Decimal("1")
    for event in events:
        total *= event.share_multiplier
    return total


def reconcile_share_basis(
    *,
    original_shares: Decimal,
    shares_fact_date: date,
    price_date: date,
    knowledge_time: datetime,
    coverage: StockSplitCoverage | None,
    events: list[StockSplitEvent],
) -> ShareBasisReconciliation:
    """Reconcile SEC shares to the raw-price share basis using verified splits."""
    warnings: list[str] = []

    if shares_fact_date >= price_date:
        return ShareBasisReconciliation(
            original_shares=original_shares,
            reconciled_shares=original_shares,
            share_multiplier=Decimal("1"),
            events=(),
            coverage=coverage,
            warnings=tuple(warnings),
        )

    if coverage is None or not coverage_covers_gap(
        coverage,
        shares_fact_date=shares_fact_date,
        price_date=price_date,
    ):
        reason = "split_coverage_unavailable"
        if coverage is not None and coverage.status.value == "error":
            reason = "split_coverage_error"
        elif coverage is not None and coverage.status.value == "unknown":
            reason = "split_coverage_unknown"
        elif coverage is not None and coverage.coverage_end < price_date:
            reason = "split_coverage_ends_before_price_date"
        elif coverage is not None and coverage.coverage_start > shares_fact_date + timedelta(
            days=1
        ):
            reason = "split_coverage_starts_too_late"
        return ShareBasisReconciliation(
            original_shares=original_shares,
            reconciled_shares=original_shares,
            share_multiplier=Decimal("1"),
            events=(),
            coverage=coverage,
            warnings=tuple(warnings),
            unavailable_reason=reason,
        )

    selected, conflict = events_in_gap(
        events,
        shares_fact_date=shares_fact_date,
        price_date=price_date,
        knowledge_time=knowledge_time,
    )
    if conflict is not None:
        return ShareBasisReconciliation(
            original_shares=original_shares,
            reconciled_shares=original_shares,
            share_multiplier=Decimal("1"),
            events=(),
            coverage=coverage,
            warnings=tuple(warnings),
            unavailable_reason=conflict,
        )

    multiplier = cumulative_share_multiplier(selected)
    reconciled = original_shares * multiplier
    if selected:
        warnings.append(SPLIT_HISTORY_NON_VINTAGE_WARNING)
        warnings.append(SPLIT_ELIGIBILITY_CONVENTION_WARNING)
    else:
        # Verified empty interval still uses retrospectively retrieved coverage.
        warnings.append(SPLIT_HISTORY_NON_VINTAGE_WARNING)

    return ShareBasisReconciliation(
        original_shares=original_shares,
        reconciled_shares=reconciled,
        share_multiplier=multiplier,
        events=selected,
        coverage=coverage,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SplitRatioError(f"{label} is not a valid decimal: {value!r}") from exc
    if parsed <= 0:
        raise SplitRatioError(f"{label} must be positive, got {parsed}")
    return parsed


def _nearly_equal(left: Decimal, right: Decimal, *, tol: Decimal = Decimal("1e-9")) -> bool:
    if left == right:
        return True
    scale = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) <= tol * scale
