"""Derived metrics and YTD-to-standalone quarterly derivation."""

from __future__ import annotations

from datetime import datetime

from equitytrace.financials.mappings import DERIVED_DEFINITIONS
from equitytrace.financials.models import CanonicalValue, FinancialPeriod, ValueProvenance
from equitytrace.financials.periods import prior_quarter_same_year
from equitytrace.financials.selector import select_canonical_value, select_ytd_value
from equitytrace.models import FinancialFact


def derive_standalone_quarter(
    *,
    concept: str,
    period: FinancialPeriod,
    facts: list[FinancialFact],
    as_of: datetime,
) -> CanonicalValue | None:
    """
    Derive a standalone quarterly value from cumulative YTD facts.

    Q2 = six-month YTD - Q1
    Q3 = nine-month YTD - six-month YTD
    Q4 is not casually derived from annual - nine-month.
    """
    if period.kind != "quarterly":
        return None
    quarter = int(period.fiscal_period[1])
    if quarter == 1 or quarter == 4:
        return None

    current_ytd = select_ytd_value(
        concept=concept,
        period=period,
        facts=facts,
        as_of=as_of,
    )
    if current_ytd.value.value is None or current_ytd.used_fact is None:
        return None

    if quarter == 2:
        prior = FinancialPeriod(
            fiscal_year=period.fiscal_year,
            fiscal_period="Q1",
            kind="quarterly",
        )
        prior_result = select_canonical_value(
            concept=concept,
            period=prior,
            facts=facts,
            as_of=as_of,
            prefer_standalone_quarter=True,
        )
        if prior_result.value.value is None:
            return None
        prior_value = prior_result.value
        derivation = "Q2_YTD - Q1"
        inputs = ("Q2_YTD", "Q1")
    else:  # Q3
        prior = FinancialPeriod(
            fiscal_year=period.fiscal_year,
            fiscal_period="Q2",
            kind="quarterly",
        )
        prior_ytd = select_ytd_value(
            concept=concept,
            period=prior,
            facts=facts,
            as_of=as_of,
        )
        if prior_ytd.value.value is None:
            return None
        prior_value = prior_ytd.value
        derivation = "Q3_YTD - Q2_YTD"
        inputs = ("Q3_YTD", "Q2_YTD")

    if current_ytd.value.unit != prior_value.unit:
        return CanonicalValue(
            concept=concept,
            value=None,
            unit=current_ytd.value.unit,
            missing_reason="Unit mismatch during YTD derivation",
            warnings=("unit_mismatch",),
        )

    # Period boundary check: prior end should be on/before current YTD end,
    # and prior start should align with current YTD start when both known.
    cur_p = current_ytd.value.provenance
    prior_p = prior_value.provenance
    if cur_p.period_end and prior_p.period_end and prior_p.period_end > cur_p.period_end:
        return CanonicalValue(
            concept=concept,
            value=None,
            missing_reason="Period boundaries do not align for YTD derivation",
            warnings=("period_mismatch",),
        )

    # For Q2: Q1 start should match YTD start (same fiscal-year origin).
    # For Q3: six-month YTD start should match nine-month YTD start.
    # Allow a small calendar tolerance for 52/53-week fiscal calendars.
    if cur_p.period_start and prior_p.period_start:
        start_gap = abs((cur_p.period_start - prior_p.period_start).days)
        if start_gap > 7:
            return CanonicalValue(
                concept=concept,
                value=None,
                missing_reason=(
                    f"YTD derivation rejected: period starts differ by {start_gap} days (limit 7)"
                ),
                warnings=("period_mismatch", "ytd_start_mismatch"),
            )

    assert current_ytd.value.value is not None
    assert prior_value.value is not None
    result_value = current_ytd.value.value - prior_value.value
    available = [dt for dt in (cur_p.available_at, prior_p.available_at) if dt is not None]
    accessions = [acc for acc in (cur_p.accession_number, prior_p.accession_number) if acc]
    return CanonicalValue(
        concept=concept,
        value=result_value,
        unit=current_ytd.value.unit,
        provenance=ValueProvenance(
            source_concept=cur_p.source_concept,
            taxonomy=cur_p.taxonomy,
            unit=current_ytd.value.unit,
            accession_number=cur_p.accession_number,
            form=cur_p.form,
            period_start=prior_p.period_end,
            period_end=cur_p.period_end,
            filing_date=cur_p.filing_date,
            acceptance_datetime=cur_p.acceptance_datetime,
            available_at=max(available) if available else None,
            is_derived=True,
            derivation=derivation,
            derivation_inputs=inputs,
            derivation_accessions=tuple(dict.fromkeys(accessions)),
        ),
        warnings=("derived_quarterly_from_ytd",),
    )


def compute_derived_values(
    resolved: dict[str, CanonicalValue],
) -> dict[str, CanonicalValue]:
    """Compute transparent derived metrics from resolved canonical values."""
    out: dict[str, CanonicalValue] = {}

    out["free_cash_flow"] = _binary_diff(
        name="free_cash_flow",
        left=resolved.get("operating_cash_flow"),
        right=resolved.get("capital_expenditures"),
        derivation="operating_cash_flow - capital_expenditures",
    )
    cash = resolved.get("cash_and_equivalents")
    sti = resolved.get("short_term_investments")
    debt = resolved.get("total_debt")
    cash_total = _sum_optional(cash, sti)
    if debt and debt.value is not None and cash_total is not None:
        out["net_debt"] = CanonicalValue(
            concept="net_debt",
            value=debt.value - cash_total,
            unit="USD",
            provenance=ValueProvenance(
                is_derived=True,
                derivation="total_debt - cash_and_equivalents - short_term_investments",
                derivation_inputs=_present_names(
                    ("total_debt", "cash_and_equivalents", "short_term_investments"),
                    resolved,
                ),
                available_at=_max_available(debt, cash, sti),
                unit="USD",
            ),
        )
    else:
        out["net_debt"] = _missing(
            "net_debt",
            "Requires total_debt and cash_and_equivalents",
        )

    out["working_capital"] = _binary_diff(
        name="working_capital",
        left=resolved.get("total_current_assets"),
        right=resolved.get("total_current_liabilities"),
        derivation="total_current_assets - total_current_liabilities",
    )

    equity = resolved.get("stockholders_equity")
    if debt and debt.value is not None and equity and equity.value is not None:
        invested = debt.value + equity.value
        inputs = ["total_debt", "stockholders_equity"]
        if cash and cash.value is not None:
            invested -= cash.value
            inputs.append("cash_and_equivalents")
        if sti and sti.value is not None:
            invested -= sti.value
            inputs.append("short_term_investments")
        out["invested_capital"] = CanonicalValue(
            concept="invested_capital",
            value=invested,
            unit="USD",
            provenance=ValueProvenance(
                is_derived=True,
                derivation="total_debt + stockholders_equity - cash - STI",
                derivation_inputs=tuple(inputs),
                available_at=_max_available(debt, equity, cash, sti),
                unit="USD",
            ),
        )
    else:
        out["invested_capital"] = _missing(
            "invested_capital",
            "Requires total_debt and stockholders_equity",
        )

    opinc = resolved.get("operating_income")
    da = resolved.get("depreciation_and_amortization")
    if opinc and opinc.value is not None and da and da.value is not None:
        out["ebitda"] = CanonicalValue(
            concept="ebitda",
            value=opinc.value + da.value,
            unit="USD",
            provenance=ValueProvenance(
                is_derived=True,
                derivation="operating_income + depreciation_and_amortization",
                derivation_inputs=("operating_income", "depreciation_and_amortization"),
                available_at=_max_available(opinc, da),
                unit="USD",
            ),
        )
    else:
        out["ebitda"] = _missing(
            "ebitda",
            "Requires operating_income and depreciation_and_amortization",
        )

    tax = resolved.get("income_tax_expense")
    if opinc and opinc.value is not None and tax and tax.value is not None:
        # Transparent NOPAT: only when operating income is positive and tax
        # expense can form a plausible rate in [0, 1].
        if opinc.value <= 0:
            out["nopat"] = _missing(
                "nopat",
                "NOPAT not calculated when operating_income <= 0",
            )
        else:
            tax_rate = tax.value / opinc.value
            if 0.0 <= tax_rate <= 1.0:
                out["nopat"] = CanonicalValue(
                    concept="nopat",
                    value=opinc.value * (1.0 - tax_rate),
                    unit="USD",
                    provenance=ValueProvenance(
                        is_derived=True,
                        derivation="operating_income * (1 - income_tax_expense/operating_income)",
                        derivation_inputs=("operating_income", "income_tax_expense"),
                        available_at=_max_available(opinc, tax),
                        unit="USD",
                    ),
                )
            else:
                out["nopat"] = _missing(
                    "nopat",
                    "Implied tax rate outside [0, 1]; NOPAT not invented",
                )
    else:
        out["nopat"] = _missing(
            "nopat",
            "Requires operating_income and income_tax_expense",
        )

    # Ensure every documented derived concept is represented.
    for name in DERIVED_DEFINITIONS:
        out.setdefault(name, _missing(name, "Not calculated"))
    return out


def _binary_diff(
    *,
    name: str,
    left: CanonicalValue | None,
    right: CanonicalValue | None,
    derivation: str,
) -> CanonicalValue:
    if left is None or left.value is None or right is None or right.value is None:
        return _missing(name, f"Requires inputs for {derivation}")
    if left.unit and right.unit and left.unit != right.unit:
        return CanonicalValue(
            concept=name,
            value=None,
            missing_reason="Unit mismatch",
            warnings=("unit_mismatch",),
        )
    return CanonicalValue(
        concept=name,
        value=left.value - right.value,
        unit=left.unit or right.unit,
        provenance=ValueProvenance(
            is_derived=True,
            derivation=derivation,
            derivation_inputs=(left.concept, right.concept),
            derivation_accessions=tuple(
                dict.fromkeys(
                    acc
                    for acc in (
                        left.provenance.accession_number,
                        right.provenance.accession_number,
                    )
                    if acc
                )
            ),
            available_at=_max_available(left, right),
            unit=left.unit or right.unit,
            accession_number=left.provenance.accession_number,
        ),
    )


def _missing(name: str, reason: str) -> CanonicalValue:
    return CanonicalValue(
        concept=name,
        value=None,
        missing_reason=reason,
        warnings=("missing_value",),
    )


def _sum_optional(
    *values: CanonicalValue | None,
) -> float | None:
    total = 0.0
    seen = False
    for value in values:
        if value is not None and value.value is not None:
            total += value.value
            seen = True
    return total if seen else None


def _present_names(
    names: tuple[str, ...],
    resolved: dict[str, CanonicalValue],
) -> tuple[str, ...]:
    return tuple(name for name in names if name in resolved and resolved[name].value is not None)


def _max_available(*values: CanonicalValue | None) -> datetime | None:
    stamps = [
        v.provenance.available_at
        for v in values
        if v is not None and v.provenance.available_at is not None
    ]
    return max(stamps) if stamps else None


# Re-export helper used by service for prior-quarter lookups.
__all__ = [
    "compute_derived_values",
    "derive_standalone_quarter",
    "prior_quarter_same_year",
]
