"""Independent adversarial checks for incomplete and comparative financial evidence."""

from datetime import date
from itertools import permutations

import pytest

from equitytrace.database import Database
from equitytrace.financials.derived import compute_derived_values
from equitytrace.financials.models import CanonicalValue, ValueProvenance
from equitytrace.financials.service import FinancialsService
from helpers.financial_fixtures import dt, make_fact, seed_company


def test_net_debt_requires_cash_even_when_investments_are_present() -> None:
    resolved = {
        "total_debt": CanonicalValue(concept="total_debt", value=100, unit="USD"),
        "short_term_investments": CanonicalValue(
            concept="short_term_investments", value=20, unit="USD"
        ),
    }
    assert compute_derived_values(resolved)["net_debt"].value is None
    # A reported zero is evidence; a missing cash tag is not.
    resolved["cash_and_equivalents"] = CanonicalValue(
        concept="cash_and_equivalents", value=0, unit="USD"
    )
    assert compute_derived_values(resolved)["net_debt"].value == 80
    resolved["cash_and_equivalents"] = CanonicalValue(
        concept="cash_and_equivalents", value=30, unit="USD"
    )
    assert compute_derived_values(resolved)["net_debt"].value == 50


@pytest.mark.parametrize("reverse_ingestion", [False, True])
def test_current_debt_ignores_comparative_rows(db: Database, reverse_ingestion: bool) -> None:
    facts = [
        make_fact(
            cik="0001000099",
            concept=concept,
            value=value,
            end=end,
            available_at=dt(2024, 2, 2) if end.year == 2022 else dt(2024, 2, 1),
            accession="0001000099-24-000001",
            form="10-K",
            fiscal_year=2023,
            fiscal_period="FY",
        )
        for concept, value, end in [
            ("ShortTermBorrowings", 10, date(2022, 12, 31)),
            ("ShortTermBorrowings", 30, date(2023, 12, 31)),
            ("LongTermDebtCurrent", 5, date(2022, 12, 31)),
            ("LongTermDebtCurrent", 7, date(2023, 12, 31)),
        ]
    ]
    with db.session() as conn:
        seed_company(
            conn,
            ticker="AUDIT",
            cik="0001000099",
            legal_name="Audit comparison case",
            facts=list(reversed(facts)) if reverse_ingestion else facts,
        )
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2023", as_of=dt(2024, 3, 1))

    assert snap.require_number("short_term_debt") == 37


def test_component_selection_is_independent_of_fact_order() -> None:
    from equitytrace.financials.models import FinancialPeriod
    from equitytrace.financials.selector import component_short_term_debt

    facts = [
        make_fact(
            cik="0001000099",
            concept="ShortTermBorrowings",
            value=value,
            end=end,
            available_at=dt(2024, 2, 1),
            accession="0001000099-24-000001",
            form="10-K",
            fiscal_year=2023,
            fiscal_period="FY",
        )
        for value, end in [(10, date(2022, 12, 31)), (30, date(2023, 12, 31))]
    ]
    current_zero = (
        facts[1]
        .model_copy(update={"concept": "LongTermDebtCurrent", "value": 0.0, "fact_id": None})
        .with_fact_id()
    )
    for ordered in permutations(facts):
        result = component_short_term_debt(
            facts=[*ordered, current_zero],
            period=FinancialPeriod(fiscal_year=2023, fiscal_period="FY", kind="annual"),
            as_of=dt(2024, 3, 1),
        )
        assert result is not None
        assert result.value == 30


@pytest.mark.parametrize("missing_side", ["short", "long"])
def test_total_debt_does_not_invent_a_missing_component(missing_side: str) -> None:
    from equitytrace.financials.selector import component_total_debt

    provenance = ValueProvenance(
        period_end=date(2023, 12, 31), source_concept="LongTermDebtNoncurrent"
    )
    known = CanonicalValue(concept="debt", value=100, unit="USD", provenance=provenance)
    missing = CanonicalValue(concept="debt", value=None, unit="USD")
    result = component_total_debt(
        short_term=missing if missing_side == "short" else known,
        long_term=missing if missing_side == "long" else known,
    )
    assert result is None
    # Explicit zero allows a complete sum.
    zero = CanonicalValue(concept="debt", value=0, unit="USD", provenance=provenance)
    complete = component_total_debt(short_term=zero, long_term=known)
    assert complete is not None
    assert complete.value == 100


def test_hand_calculated_drift_and_costs() -> None:
    from equitytrace.portfolio.engine import apply_interval_returns, apply_weight_transition
    from equitytrace.portfolio.models import PortfolioState

    state = PortfolioState(nav=1000, cash_weight=1, asset_weights={})
    entry = apply_weight_transition(state, {"A": 0.5, "B": 0.5}, target_cash=0, cost_rate=0.01)
    assert entry.cost_amount == 10
    apply_interval_returns(state, {"A": 0.2, "B": -0.1})
    assert state.nav == pytest.approx(1039.5)
    assert state.asset_weights == pytest.approx({"A": 4 / 7, "B": 3 / 7})
    switch = apply_weight_transition(state, {"B": 1}, target_cash=0, cost_rate=0.01)
    assert switch.gross_turnover == pytest.approx(8 / 7)
    assert switch.cost_amount == pytest.approx(11.88)
    assert state.nav == pytest.approx(1027.62)


def test_missing_debt_component_cannot_create_a_deleveraging_signal(db: Database) -> None:
    from equitytrace.factors.engine import FactorEngine
    from helpers.financial_fixtures import alpha_facts

    facts = [
        fact
        for fact in alpha_facts()
        if not (fact.fiscal_year == 2023 and fact.concept == "LongTermDebtNoncurrent")
    ]
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Missing debt case",
            facts=facts,
        )
        result = FactorEngine(conn).calculate(
            "ALPHA", "debt_change", "FY2023", as_of=dt(2024, 3, 1), persist=False
        )
    assert not result.valid
    assert result.value is None
    assert result.inputs["total_debt"] is None
    assert result.inputs["prior_total_debt"] == 50
