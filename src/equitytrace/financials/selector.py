"""Point-in-time canonical concept selection with deterministic conflict handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from equitytrace.financials.mappings import (
    CANONICAL_MAPPINGS,
    CanonicalMapping,
    PeriodType,
    apply_sign,
)
from equitytrace.financials.models import CanonicalValue, FinancialPeriod, ValueProvenance
from equitytrace.financials.periods import (
    duration_days,
    looks_like_ytd_duration,
    period_matches_fact,
)
from equitytrace.models import FinancialFact


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Outcome of selecting one canonical concept for a period."""

    value: CanonicalValue
    used_fact: FinancialFact | None = None
    candidates_considered: int = 0


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    fact: FinancialFact
    mapping_priority: int
    score: tuple[object, ...]
    normalized_value: float


def select_canonical_value(
    *,
    concept: str,
    period: FinancialPeriod,
    facts: list[FinancialFact],
    as_of: datetime,
    prefer_standalone_quarter: bool = True,
) -> SelectionResult:
    """
    Select the best point-in-time fact for a canonical concept.

    Selection never uses facts with ``available_at > as_of`` (caller should
    pre-filter; this function also enforces the rule). Conflicts among equal
    top scores with differing values produce a warning and a deterministic pick.
    """
    mapping = CANONICAL_MAPPINGS[concept]
    eligible = [
        f
        for f in facts
        if f.available_at <= as_of
        and f.concept in mapping.candidates
        and _unit_matches(f.unit, mapping.expected_unit)
        and _period_type_ok(f, mapping)
    ]

    exact = [f for f in eligible if period_matches_fact(period, f.fiscal_year, f.fiscal_period)]

    warnings: list[str] = []
    if not exact:
        return SelectionResult(
            value=CanonicalValue(
                concept=concept,
                value=None,
                unit=mapping.expected_unit,
                missing_reason="No matching facts available at as_of for period",
                warnings=("missing_value",),
            ),
            candidates_considered=len(eligible),
        )

    scored: list[_ScoredCandidate] = []
    for fact in exact:
        days = duration_days(fact.start_date, fact.end_date)
        is_ytd = looks_like_ytd_duration(days, period)
        if prefer_standalone_quarter and is_ytd and period.kind == "quarterly":
            # Leave YTD facts for the derivation layer.
            continue
        priority = mapping.candidates.index(fact.concept)
        score = _score_fact(fact, mapping=mapping, period=period, is_ytd=is_ytd)
        scored.append(
            _ScoredCandidate(
                fact=fact,
                mapping_priority=priority,
                score=score,
                normalized_value=apply_sign(fact.value, mapping.sign_mode),
            )
        )

    if not scored:
        return SelectionResult(
            value=CanonicalValue(
                concept=concept,
                value=None,
                unit=mapping.expected_unit,
                missing_reason="Only cumulative/YTD facts matched; standalone unavailable",
                warnings=("ytd_only", "missing_value"),
            ),
            candidates_considered=len(eligible),
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    best = scored[0]
    ties = [c for c in scored if c.score == best.score]
    if len(ties) > 1:
        values = {round(c.normalized_value, 6) for c in ties}
        if len(values) > 1:
            warnings.append("conflicting_concepts")
            # Deterministic tie-break: higher mapping priority (lower index),
            # then latest available_at already in score, then accession, concept.
            ties.sort(
                key=lambda c: (
                    -c.mapping_priority,
                    c.fact.accession_number or "",
                    c.fact.concept,
                    c.fact.fact_id or "",
                ),
                reverse=True,
            )
            best = ties[0]
            warnings.append(
                "conflict_resolved_deterministically:"
                f"{best.fact.concept}:{best.fact.accession_number}"
            )
        else:
            # Same value from multiple equal candidates — keep richest provenance.
            ties.sort(
                key=lambda c: (
                    c.fact.available_at,
                    c.fact.accession_number or "",
                ),
                reverse=True,
            )
            best = ties[0]

    form = (best.fact.form or "").upper()
    is_restated = form.endswith("/A")
    if is_restated:
        warnings.append("restated_value")

    normalized = best.normalized_value
    reported = best.fact.value
    sign_changed = normalized != reported
    if sign_changed and mapping.sign_mode.value != "identity":
        warnings.append(f"sign_normalized:{mapping.sign_mode.value}")

    provenance = ValueProvenance(
        source_concept=best.fact.concept,
        taxonomy=best.fact.taxonomy,
        unit=best.fact.unit,
        accession_number=best.fact.accession_number,
        form=best.fact.form,
        period_start=best.fact.start_date,
        period_end=best.fact.end_date,
        filing_date=best.fact.filing_date,
        acceptance_datetime=best.fact.acceptance_datetime,
        available_at=best.fact.available_at,
        fact_id=best.fact.fact_id,
        is_derived=False,
        mapping_priority=best.mapping_priority,
        is_restated=is_restated,
        reported_value=reported,
        sign_mode=mapping.sign_mode.value,
        normalized_from_sign=sign_changed,
    )
    return SelectionResult(
        value=CanonicalValue(
            concept=concept,
            value=normalized,
            unit=best.fact.unit,
            provenance=provenance,
            warnings=tuple(warnings),
        ),
        used_fact=best.fact,
        candidates_considered=len(eligible),
    )


def select_ytd_value(
    *,
    concept: str,
    period: FinancialPeriod,
    facts: list[FinancialFact],
    as_of: datetime,
) -> SelectionResult:
    """Select a cumulative year-to-date fact for quarterly derivation."""
    mapping = CANONICAL_MAPPINGS[concept]
    exact = [
        f
        for f in facts
        if f.available_at <= as_of
        and f.concept in mapping.candidates
        and _unit_matches(f.unit, mapping.expected_unit)
        and period_matches_fact(period, f.fiscal_year, f.fiscal_period)
        and looks_like_ytd_duration(duration_days(f.start_date, f.end_date), period)
    ]
    if not exact and period.kind == "quarterly" and period.fiscal_period in {"Q2", "Q3"}:
        # Some issuers tag YTD with the quarter fp without long duration metadata;
        # accept exact fp matches that were skipped as non-standalone only when
        # duration clearly exceeds a quarter.
        exact = [
            f
            for f in facts
            if f.available_at <= as_of
            and f.concept in mapping.candidates
            and _unit_matches(f.unit, mapping.expected_unit)
            and period_matches_fact(period, f.fiscal_year, f.fiscal_period)
            and (duration_days(f.start_date, f.end_date) or 0) > 130
        ]
    if not exact:
        return SelectionResult(
            value=CanonicalValue(
                concept=concept,
                value=None,
                unit=mapping.expected_unit,
                missing_reason="No YTD fact available",
                warnings=("missing_value",),
            )
        )

    scored = sorted(
        exact,
        key=lambda f: _score_fact(
            f,
            mapping=mapping,
            period=period,
            is_ytd=True,
        ),
        reverse=True,
    )
    best = scored[0]
    normalized = apply_sign(best.value, mapping.sign_mode)
    return SelectionResult(
        value=CanonicalValue(
            concept=concept,
            value=normalized,
            unit=best.unit,
            provenance=ValueProvenance(
                source_concept=best.concept,
                taxonomy=best.taxonomy,
                unit=best.unit,
                accession_number=best.accession_number,
                form=best.form,
                period_start=best.start_date,
                period_end=best.end_date,
                filing_date=best.filing_date,
                acceptance_datetime=best.acceptance_datetime,
                available_at=best.available_at,
                fact_id=best.fact_id,
                is_derived=False,
                mapping_priority=mapping.candidates.index(best.concept),
                is_restated=(best.form or "").upper().endswith("/A"),
                reported_value=best.value,
                sign_mode=mapping.sign_mode.value,
                normalized_from_sign=normalized != best.value,
            ),
        ),
        used_fact=best,
        candidates_considered=len(exact),
    )


def _unit_matches(actual: str, expected: str) -> bool:
    return actual.strip().lower() == expected.strip().lower()


def _period_type_ok(fact: FinancialFact, mapping: CanonicalMapping) -> bool:
    has_start = fact.start_date is not None
    if mapping.period_type is PeriodType.INSTANT:
        return not has_start or fact.start_date == fact.end_date
    if mapping.period_type is PeriodType.DURATION:
        # Duration concepts should generally have a start; allow missing start
        # only when end exists (some cash-flow tags omit start in sparse data).
        return fact.end_date is not None
    return True


def _score_fact(
    fact: FinancialFact,
    *,
    mapping: CanonicalMapping,
    period: FinancialPeriod,
    is_ytd: bool,
) -> tuple[object, ...]:
    """
    Higher tuple sorts first.

    Prefer:
    1. exact period already filtered
    2. non-YTD for standalone requests
    3. annual 10-K for FY / 10-Q for quarters
    4. duration length consistent with annual (~365d) or quarter (~90d)
    5. latest period end date (10-K comparative years often share fy/fp)
    6. latest available_at
    7. higher-priority mapping (lower index)
    8. amendments only as available (already filtered by as_of)
    """
    form = (fact.form or "").upper()
    is_amendment = 1 if form.endswith("/A") else 0
    base_form = form[:-2] if is_amendment else form

    if period.kind == "annual":
        form_score = 2 if base_form == "10-K" else 1 if base_form.startswith("10-K") else 0
    else:
        form_score = 2 if base_form == "10-Q" else 1 if base_form.startswith("10-Q") else 0

    priority = mapping.candidates.index(fact.concept)
    days = duration_days(fact.start_date, fact.end_date)
    if mapping.period_type is PeriodType.INSTANT:
        duration_score = 1
    elif period.kind == "annual":
        # Prefer single-year annual spans; reject multi-year comparatives later
        # via lower score when days are far from ~365.
        if days is None:
            duration_score = 0
        elif 340 <= days <= 380:
            duration_score = 2
        elif 300 <= days <= 420:
            duration_score = 1
        else:
            duration_score = 0
    else:
        if is_ytd:
            duration_score = 1
        elif days is None:
            duration_score = 0
        elif 70 <= days <= 110:
            duration_score = 2
        elif days <= 130:
            duration_score = 1
        else:
            duration_score = 0

    end_key = fact.end_date or date.min
    # Accession is intentionally excluded from the primary score so equal-quality
    # candidates with differing values can be detected as conflicts.
    return (
        0 if is_ytd else 1,
        form_score,
        duration_score,
        end_key,
        fact.available_at,
        -priority,
        is_amendment,
    )


def component_total_debt(
    *,
    short_term: CanonicalValue | None,
    long_term: CanonicalValue | None,
) -> CanonicalValue | None:
    """Calculate total debt from components when direct tag is missing."""
    if short_term is None and long_term is None:
        return None
    if (short_term is None or short_term.value is None) and (
        long_term is None or long_term.value is None
    ):
        return None
    st = short_term.value if short_term and short_term.value is not None else 0.0
    lt = long_term.value if long_term and long_term.value is not None else 0.0
    inputs: list[str] = []
    accessions: list[str] = []
    available: list[datetime] = []
    if short_term and short_term.value is not None:
        inputs.append("short_term_debt")
        if short_term.provenance.accession_number:
            accessions.append(short_term.provenance.accession_number)
        if short_term.provenance.available_at:
            available.append(short_term.provenance.available_at)
    if long_term and long_term.value is not None:
        inputs.append("long_term_debt")
        if long_term.provenance.accession_number:
            accessions.append(long_term.provenance.accession_number)
        if long_term.provenance.available_at:
            available.append(long_term.provenance.available_at)
    return CanonicalValue(
        concept="total_debt",
        value=st + lt,
        unit="USD",
        provenance=ValueProvenance(
            is_derived=True,
            derivation="short_term_debt + long_term_debt",
            derivation_inputs=tuple(inputs),
            derivation_accessions=tuple(dict.fromkeys(accessions)),
            accession_number=accessions[0] if accessions else None,
            available_at=max(available) if available else None,
            unit="USD",
        ),
        warnings=("derived_from_components",),
    )


def component_short_term_debt(
    *,
    facts: list[FinancialFact],
    period: FinancialPeriod,
    as_of: datetime,
) -> CanonicalValue | None:
    """
    Sum non-overlapping short-term debt components when DebtCurrent is absent.

    Uses ShortTermBorrowings (or CommercialPaper) + LongTermDebtCurrent.
    Does not sum CommercialPaper on top of ShortTermBorrowings.
    """
    from equitytrace.financials.periods import period_matches_fact as _match

    eligible = [
        f
        for f in facts
        if f.available_at <= as_of
        and f.unit.strip().lower() == "usd"
        and _match(period, f.fiscal_year, f.fiscal_period)
        and f.concept
        in {
            "ShortTermBorrowings",
            "CommercialPaper",
            "LongTermDebtCurrent",
        }
        and (f.start_date is None or f.start_date == f.end_date)
    ]
    if not eligible:
        return None

    # Latest available fact per concept.
    by_concept: dict[str, FinancialFact] = {}
    for fact in sorted(eligible, key=lambda f: f.available_at, reverse=True):
        by_concept.setdefault(fact.concept, fact)

    borrowings = by_concept.get("ShortTermBorrowings") or by_concept.get("CommercialPaper")
    current_ltd = by_concept.get("LongTermDebtCurrent")
    if borrowings is None and current_ltd is None:
        return None

    total = 0.0
    inputs: list[str] = []
    accessions: list[str] = []
    available: list[datetime] = []
    end: date | None = None
    component_pairs: list[tuple[str, FinancialFact | None]] = [
        ("short_term_borrowings_or_cp", borrowings),
        ("long_term_debt_current", current_ltd),
    ]
    for label, component in component_pairs:
        if component is None:
            continue
        total += component.value
        inputs.append(f"{label}:{component.concept}")
        if component.accession_number:
            accessions.append(component.accession_number)
        available.append(component.available_at)
        end = component.end_date or end

    return CanonicalValue(
        concept="short_term_debt",
        value=total,
        unit="USD",
        provenance=ValueProvenance(
            is_derived=True,
            derivation="sum_nonoverlapping_current_debt_components",
            derivation_inputs=tuple(inputs),
            derivation_accessions=tuple(dict.fromkeys(accessions)),
            accession_number=accessions[0] if accessions else None,
            available_at=max(available) if available else None,
            period_end=end,
            unit="USD",
        ),
        warnings=("derived_from_components",),
    )


def component_gross_profit(
    *,
    revenue: CanonicalValue | None,
    cost: CanonicalValue | None,
) -> CanonicalValue | None:
    if (
        revenue is None
        or revenue.value is None
        or cost is None
        or cost.value is None
        or revenue.unit != cost.unit
    ):
        return None
    available = [
        dt
        for dt in (revenue.provenance.available_at, cost.provenance.available_at)
        if dt is not None
    ]
    accessions = [
        acc
        for acc in (
            revenue.provenance.accession_number,
            cost.provenance.accession_number,
        )
        if acc
    ]
    return CanonicalValue(
        concept="gross_profit",
        value=revenue.value - cost.value,
        unit=revenue.unit,
        provenance=ValueProvenance(
            is_derived=True,
            derivation="revenue - cost_of_revenue",
            derivation_inputs=("revenue", "cost_of_revenue"),
            derivation_accessions=tuple(dict.fromkeys(accessions)),
            accession_number=revenue.provenance.accession_number,
            available_at=max(available) if available else None,
            unit=revenue.unit,
            period_end=revenue.provenance.period_end,
            period_start=revenue.provenance.period_start,
        ),
        warnings=("derived_from_components",),
    )


def earliest_period_bounds(
    values: list[CanonicalValue],
) -> tuple[date | None, date | None]:
    """
    Infer snapshot period start/end from selected values.

    Prefer duration facts whose span looks like a single annual/quarter period
    so comparative prior-year rows in the same filing do not widen bounds.
    """
    duration_values: list[CanonicalValue] = []
    for value in values:
        if value.provenance.period_end is None:
            continue
        days = duration_days(value.provenance.period_start, value.provenance.period_end)
        if days is None or days <= 420:
            duration_values.append(value)
    pool = duration_values or values
    starts = [v.provenance.period_start for v in pool if v.provenance.period_start]
    ends = [v.provenance.period_end for v in pool if v.provenance.period_end]
    return (min(starts) if starts else None, max(ends) if ends else None)
