"""Debt fallbacks require complete components on one evidenced balance date."""

from datetime import date

import pytest

from equitytrace.database import Database
from equitytrace.factors.engine import FactorEngine
from equitytrace.financials.models import CanonicalValue, ValueProvenance
from equitytrace.financials.selector import component_total_debt
from equitytrace.financials.service import FinancialsService
from equitytrace.models import FinancialFact
from helpers.financial_fixtures import dt, make_fact, seed_company


def _facts(rows: list[tuple[str, float, date]], *, quarterly: bool = False) -> list[FinancialFact]:
    return [
        make_fact(
            cik="0001000099",
            concept=concept,
            value=value,
            end=end,
            available_at=dt(2024, 2, 1),
            accession="audit-current",
            form="10-Q" if quarterly else "10-K",
            fiscal_year=2023,
            fiscal_period="Q2" if quarterly else "FY",
        )
        for concept, value, end in rows
    ]


def _seed(conn: object, facts: list[FinancialFact], reverse: bool) -> None:
    seed_company(
        conn,
        ticker="AUDIT",
        cik="0001000099",
        legal_name="Debt consistency case",
        facts=list(reversed(facts)) if reverse else facts,
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("case", ["comparative", "missing", "missing_borrowings", "both_prior"])
def test_incomplete_current_debt_is_unavailable(db: Database, reverse: bool, case: str) -> None:
    current, prior = date(2023, 12, 31), date(2022, 12, 31)
    rows = [("LongTermDebtNoncurrent", 40, current)]
    if case != "missing_borrowings":
        rows.append(("ShortTermBorrowings", 30, prior if case == "both_prior" else current))
    if case != "missing":
        rows.append(("LongTermDebtCurrent", 5, current if case == "missing_borrowings" else prior))
    facts = _facts(rows)
    facts.append(
        make_fact(
            cik="0001000099",
            concept="DebtAndCapitalLeaseObligations",
            value=100,
            end=prior,
            available_at=dt(2023, 2, 1),
            accession="audit-prior",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        )
    )
    with db.session() as conn:
        _seed(conn, facts, reverse)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2023", as_of=dt(2024, 3, 1))
        result = FactorEngine(conn).calculate(
            "AUDIT", "debt_change", "FY2023", as_of=dt(2024, 3, 1), persist=False
        )
    short = snap.balance_sheet["short_term_debt"]
    assert short.value is None, (short.value, snap.require_number("total_debt"), result.value)
    assert short.missing_reason
    assert snap.require_number("total_debt") is None
    assert not result.valid
    assert result.inputs == {"total_debt": None, "prior_total_debt": 100}
    assert result.value is None


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("quarterly", [False, True])
@pytest.mark.parametrize("borrowings,current_ltd", [(30, 7), (30, 0), (0, 7), (0, 0)])
def test_complete_same_date_debt_and_provenance(
    db: Database, reverse: bool, quarterly: bool, borrowings: float, current_ltd: float
) -> None:
    end = date(2023, 6, 30) if quarterly else date(2023, 12, 31)
    facts = _facts(
        [
            ("ShortTermBorrowings", borrowings, end),
            ("LongTermDebtCurrent", current_ltd, end),
            ("LongTermDebtNoncurrent", 40, end),
        ],
        quarterly=quarterly,
    )
    with db.session() as conn:
        _seed(conn, facts, reverse)
        snap = FinancialsService(conn).get_snapshot(
            "AUDIT", "Q2-2023" if quarterly else "FY2023", as_of=dt(2024, 3, 1)
        )
    assert snap.require_number("short_term_debt") == borrowings + current_ltd
    assert snap.require_number("total_debt") == borrowings + current_ltd + 40
    for concept in ("short_term_debt", "total_debt"):
        provenance = snap.balance_sheet[concept].provenance
        assert provenance.period_end == end
        assert provenance.available_at == dt(2024, 2, 1)
        assert provenance.derivation_accessions == ("audit-current",)


@pytest.mark.parametrize("direct", ["DebtCurrent", "DebtAndCapitalLeaseObligations"])
@pytest.mark.parametrize("value", [0, 37])
def test_complete_direct_debt_preserved(db: Database, direct: str, value: float) -> None:
    end = date(2023, 12, 31)
    facts = _facts(
        [
            ("ShortTermBorrowings", 30, end),
            ("LongTermDebtCurrent", 5, date(2022, 12, 31)),
            ("LongTermDebtNoncurrent", 40, end),
            (direct, value, end),
        ]
    )
    # A later partial disclosure does not invalidate known complete direct debt.
    facts = [
        fact.model_copy(update={"available_at": dt(2024, 2, 2)})
        if fact.concept == "ShortTermBorrowings"
        else fact
        for fact in facts
    ]
    with db.session() as conn:
        _seed(conn, facts, False)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2023", as_of=dt(2024, 3, 1))
    concept = "short_term_debt" if direct == "DebtCurrent" else "total_debt"
    assert snap.require_number(concept) == value
    assert snap.balance_sheet[concept].provenance.source_concept == direct
    if direct == "DebtCurrent":
        assert snap.require_number("total_debt") == value + 40


@pytest.mark.parametrize(
    "short_date,long_date",
    [
        (date(2022, 12, 31), date(2023, 12, 31)),
        (date(2023, 12, 31), None),
    ],
)
def test_total_debt_rejects_inconsistent_or_unknown_dates(
    short_date: date, long_date: date | None
) -> None:
    short = CanonicalValue(
        concept="short_term_debt",
        value=35,
        unit="USD",
        provenance=ValueProvenance(period_end=short_date),
    )
    long = CanonicalValue(
        concept="long_term_debt",
        value=40,
        unit="USD",
        provenance=ValueProvenance(period_end=long_date, source_concept="LongTermDebtNoncurrent"),
    )
    result = component_total_debt(short_term=short, long_term=long)
    assert result is not None
    assert result.value is None
    assert result.missing_reason


@pytest.mark.parametrize("direct", ["DebtCurrent", "DebtAndCapitalLeaseObligations"])
def test_comparative_direct_debt_does_not_bypass_missing_current_components(
    db: Database, direct: str
) -> None:
    facts = _facts(
        [
            ("ShortTermBorrowings", 30, date(2023, 12, 31)),
            ("LongTermDebtCurrent", 5, date(2022, 12, 31)),
            ("LongTermDebtNoncurrent", 40, date(2023, 12, 31)),
            (direct, 35, date(2022, 12, 31)),
        ]
    )
    with db.session() as conn:
        _seed(conn, facts, False)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2023", as_of=dt(2024, 3, 1))
    assert snap.require_number("short_term_debt") is None
    assert snap.require_number("total_debt") is None


def test_short_debt_helper_anchors_to_other_current_balance_evidence() -> None:
    from equitytrace.financials.periods import parse_period
    from equitytrace.financials.selector import component_short_term_debt

    facts = _facts(
        [
            ("ShortTermBorrowings", 30, date(2022, 12, 31)),
            ("LongTermDebtCurrent", 5, date(2022, 12, 31)),
            ("LongTermDebtNoncurrent", 40, date(2023, 12, 31)),
        ]
    )
    result = component_short_term_debt(
        facts=facts, period=parse_period("FY2023"), as_of=dt(2024, 3, 1)
    )
    assert result.value is None
    assert result.missing_reason


@pytest.mark.parametrize("concept", ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"])
def test_ambiguous_long_term_scope_cannot_be_summed(db: Database, concept: str) -> None:
    end = date(2023, 12, 31)
    facts = _facts(
        [
            ("ShortTermBorrowings", 30, end),
            ("LongTermDebtCurrent", 7, end),
            (concept, 40, end),
        ]
    )
    with db.session() as conn:
        _seed(conn, facts, False)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2023", as_of=dt(2024, 3, 1))
    assert snap.require_number("short_term_debt") == 37
    assert snap.require_number("total_debt") is None
    assert "noncurrent scope" in (snap.balance_sheet["total_debt"].missing_reason or "")
