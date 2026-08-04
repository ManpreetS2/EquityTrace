"""Canonical mapping, selection, snapshots, and restatement tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

from equitytrace.database import Database
from equitytrace.financials.mappings import CANONICAL_MAPPINGS, apply_sign, get_mapping
from equitytrace.financials.periods import parse_period, prior_comparable_period
from equitytrace.financials.selector import select_canonical_value
from equitytrace.financials.service import FinancialsService
from equitytrace.models import FinancialFact
from helpers.financial_fixtures import alpha_facts, dt, make_fact, seed_company


def test_canonical_mapping_priority_order() -> None:
    mapping = get_mapping("revenue")
    assert mapping.candidates[0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert "Revenues" in mapping.candidates
    assert mapping.candidates.index("SalesRevenueNet") < mapping.candidates.index("Revenues")


def test_parse_period_variants() -> None:
    assert parse_period("FY2023").label() == "FY2023"
    assert parse_period("Q3-2023").label() == "Q3-2023"
    assert parse_period("2023Q2").fiscal_period == "Q2"
    prior = prior_comparable_period(parse_period("FY2023"))
    assert prior.fiscal_year == 2022


def test_unit_mismatch_rejection(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        facts = [
            f
            for f in alpha_facts()
            if f.fiscal_year == 2023 and f.fiscal_period == "FY" and "Revenue" in f.concept
        ]
        result = select_canonical_value(
            concept="revenue",
            period=parse_period("FY2023"),
            facts=facts,
            as_of=dt(2023, 12, 1),
        )
    assert result.value.value == 120.0
    assert result.value.unit == "USD"


def test_instant_versus_duration_facts(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        snap = FinancialsService(conn).get_snapshot(
            "ALPHA",
            "FY2023",
            as_of=dt(2023, 12, 1),
        )
    assets = snap.balance_sheet["total_assets"]
    revenue = snap.income_statement["revenue"]
    assert assets.value == 240.0
    assert assets.provenance.period_start is None
    assert revenue.provenance.period_start == date(2022, 10, 1)


def test_annual_revenue_normalization(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        snap = FinancialsService(conn).get_snapshot("ALPHA", "FY2023", as_of=dt(2023, 12, 1))
    assert snap.income_statement["revenue"].value == 120.0
    assert (
        snap.income_statement["revenue"].provenance.source_concept
        == "RevenueFromContractWithCustomerExcludingAssessedTax"
    )


def test_quarterly_ytd_derivation(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        snap = FinancialsService(conn).get_snapshot("ALPHA", "Q2-2023", as_of=dt(2023, 6, 1))
    revenue = snap.income_statement["revenue"]
    assert revenue.value == 30.0  # 55 YTD - 25 Q1
    assert revenue.provenance.is_derived is True
    assert "derived_quarterly_from_ytd" in revenue.warnings


def test_derived_value_provenance(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        snap = FinancialsService(conn).get_snapshot("ALPHA", "FY2023", as_of=dt(2023, 12, 1))
    fcf = snap.derived["free_cash_flow"]
    assert fcf.value == 30.0  # 40 - 10
    assert fcf.provenance.is_derived is True
    assert "operating_cash_flow" in fcf.provenance.derivation_inputs
    ebitda = snap.derived["ebitda"]
    assert ebitda.value == 36.0  # 30 + 6


def test_total_debt_from_components(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        snap = FinancialsService(conn).get_snapshot("ALPHA", "FY2023", as_of=dt(2023, 12, 1))
    assert snap.balance_sheet["total_debt"].value == 40.0  # 35 + 5
    assert snap.balance_sheet["total_debt"].provenance.is_derived is True


def test_amendment_unavailable_before_acceptance(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        before = FinancialsService(conn).get_snapshot(
            "ALPHA",
            "FY2023",
            as_of=dt(2023, 12, 1),
        )
        after = FinancialsService(conn).get_snapshot(
            "ALPHA",
            "FY2023",
            as_of=dt(2024, 1, 16),
        )
    assert before.income_statement["revenue"].value == 120.0
    assert before.income_statement["revenue"].provenance.form == "10-K"
    assert after.income_statement["revenue"].value == 121.0
    assert after.income_statement["revenue"].provenance.form == "10-K/A"
    assert "restated_value" in after.income_statement["revenue"].warnings


def test_snapshot_excludes_future_filings(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        snap = FinancialsService(conn).get_snapshot(
            "ALPHA",
            "FY2023",
            as_of=datetime(2023, 10, 1, tzinfo=UTC),
        )
    # FY2023 10-K arrives Nov 2023; before that revenue should be missing.
    assert snap.income_statement["revenue"].value is None


def test_conflicting_concepts_resolve_deterministically() -> None:
    """Equal-score conflicts with different values warn and pick deterministically."""
    cik = "0001999999"
    available = dt(2023, 11, 4)
    # Two same-priority-ish tags? Use same candidate with different accessions after
    # forcing equal scores via identical metadata but different concepts at same priority
    # by using SalesRevenueNet vs a duplicate path — instead create two Revenues facts.
    facts = [
        make_fact(
            cik=cik,
            concept="Revenues",
            value=100.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=available,
            accession="0001999999-23-000001",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="Revenues",
            value=110.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=available,
            accession="0001999999-23-000002",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    result = select_canonical_value(
        concept="revenue",
        period=parse_period("FY2022"),
        facts=facts,
        as_of=dt(2023, 12, 1),
    )
    assert result.value.value is not None
    assert "conflicting_concepts" in result.value.warnings
    assert "conflict_resolved_deterministically" in ",".join(result.value.warnings)


def test_mapping_sign_normalization() -> None:
    mapping = CANONICAL_MAPPINGS["capital_expenditures"]
    assert apply_sign(-10.0, mapping.sign_mode) == 10.0


def test_exact_period_matching_ignores_other_years(db: Database) -> None:
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        snap = FinancialsService(conn).get_snapshot("ALPHA", "FY2022", as_of=dt(2023, 12, 1))
    assert snap.income_statement["revenue"].value == 100.0


def test_duration_instant_rejection_for_assets() -> None:
    assets_mapping = get_mapping("total_assets")
    assert assets_mapping.period_type.value == "instant"
    fact = FinancialFact(
        cik="0001000001",
        taxonomy="us-gaap",
        concept="Assets",
        unit="USD",
        value=1.0,
        start_date=date(2022, 1, 1),
        end_date=date(2022, 12, 31),
        available_at=dt(2023, 1, 1),
        fiscal_year=2022,
        fiscal_period="FY",
        form="10-K",
        accession_number="x",
    )
    result = select_canonical_value(
        concept="total_assets",
        period=parse_period("FY2022"),
        facts=[fact],
        as_of=dt(2023, 2, 1),
    )
    assert result.value.value is None
