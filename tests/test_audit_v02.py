"""Targeted finance-logic audit tests for EquityTrace v0.2."""

from __future__ import annotations

from datetime import date

import pytest

from equitytrace.database import Database
from equitytrace.factors.engine import FactorEngine
from equitytrace.factors.ranking import rank_factors
from equitytrace.financials.mappings import SignMode, apply_sign, get_mapping
from equitytrace.financials.periods import parse_period
from equitytrace.financials.selector import select_canonical_value
from equitytrace.financials.service import FinancialsService
from equitytrace.repositories.factors import FactorRepository
from helpers.financial_fixtures import dt, make_fact, seed_company


def _seed(
    conn: object,
    facts: list[object],
    ticker: str = "AUDIT",
    cik: str = "0001999001",
) -> None:
    seed_company(
        conn,
        ticker=ticker,
        cik=cik,
        legal_name="Audit Co",
        facts=facts,  # type: ignore[arg-type]
    )


def test_positive_capex_normalized_as_expenditure() -> None:
    mapping = get_mapping("capital_expenditures")
    assert mapping.sign_mode is SignMode.ABS
    assert apply_sign(12.0, mapping.sign_mode) == 12.0


def test_negative_capex_normalized_without_double_reverse(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="NetCashProvidedByUsedInOperatingActivities",
            value=50.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="PaymentsToAcquirePropertyPlantAndEquipment",
            value=-8.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=dt(2023, 3, 1))
    capex = snap.cash_flow["capital_expenditures"]
    assert capex.value == 8.0
    assert capex.provenance.reported_value == -8.0
    assert capex.provenance.sign_mode == "abs"
    assert capex.provenance.normalized_from_sign is True
    assert snap.derived["free_cash_flow"].value == 42.0


def test_direct_total_debt_preferred_over_components(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="DebtAndCapitalLeaseObligations",
            value=100.0,
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="ShortTermBorrowings",
            value=20.0,
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="LongTermDebtNoncurrent",
            value=90.0,
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=dt(2023, 3, 1))
    debt = snap.balance_sheet["total_debt"]
    assert debt.value == 100.0
    assert debt.provenance.is_derived is False
    assert debt.provenance.source_concept == "DebtAndCapitalLeaseObligations"


def test_total_debt_component_sum_without_double_count(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="ShortTermBorrowings",
            value=15.0,
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="LongTermDebtCurrent",
            value=5.0,
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="LongTermDebtNoncurrent",
            value=40.0,
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a-1",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=dt(2023, 3, 1))
    assert snap.balance_sheet["short_term_debt"].value == 20.0
    assert snap.balance_sheet["total_debt"].value == 60.0
    assert snap.balance_sheet["total_debt"].provenance.is_derived is True


def test_ytd_mismatched_starts_not_subtracted(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=10.0,
            start=date(2022, 1, 15),  # mismatched vs YTD start
            end=date(2022, 3, 31),
            available_at=dt(2022, 5, 1),
            accession="q1",
            form="10-Q",
            fiscal_year=2022,
            fiscal_period="Q1",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=25.0,
            start=date(2022, 1, 1),
            end=date(2022, 6, 30),
            available_at=dt(2022, 8, 1),
            accession="q2",
            form="10-Q",
            fiscal_year=2022,
            fiscal_period="Q2",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "Q2-2022", as_of=dt(2022, 9, 1))
    assert snap.income_statement["revenue"].value is None
    assert "ytd_start_mismatch" in snap.warnings or "period_mismatch" in snap.warnings


def test_q2_and_q3_aligned_ytd_derivation(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=10.0,
            start=date(2022, 1, 1),
            end=date(2022, 3, 31),
            available_at=dt(2022, 5, 1),
            accession="q1",
            form="10-Q",
            fiscal_year=2022,
            fiscal_period="Q1",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=22.0,
            start=date(2022, 1, 1),
            end=date(2022, 6, 30),
            available_at=dt(2022, 8, 1),
            accession="q2",
            form="10-Q",
            fiscal_year=2022,
            fiscal_period="Q2",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=39.0,
            start=date(2022, 1, 1),
            end=date(2022, 9, 30),
            available_at=dt(2022, 11, 1),
            accession="q3",
            form="10-Q",
            fiscal_year=2022,
            fiscal_period="Q3",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        q2 = FinancialsService(conn).get_snapshot("AUDIT", "Q2-2022", as_of=dt(2022, 12, 1))
        q3 = FinancialsService(conn).get_snapshot("AUDIT", "Q3-2022", as_of=dt(2022, 12, 1))
        q4 = FinancialsService(conn).get_snapshot("AUDIT", "Q4-2022", as_of=dt(2022, 12, 1))
    assert q2.income_statement["revenue"].value == 12.0
    assert q2.income_statement["revenue"].provenance.derivation_inputs == ("Q2_YTD", "Q1")
    assert set(q2.income_statement["revenue"].provenance.derivation_accessions) == {"q2", "q1"}
    assert q3.income_statement["revenue"].value == 17.0
    assert q3.income_statement["revenue"].provenance.derivation == "Q3_YTD - Q2_YTD"
    assert q4.income_statement["revenue"].value is None


def test_amendment_timeline_and_no_leak(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=100.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="orig",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=105.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 6, 1),
            accession="amd",
            form="10-K/A",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        before = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=dt(2023, 3, 1))
        on = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=dt(2023, 6, 1))
        after = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=dt(2023, 7, 1))
    assert before.income_statement["revenue"].value == 100.0
    assert before.income_statement["revenue"].provenance.form == "10-K"
    assert on.income_statement["revenue"].value == 105.0
    assert after.income_statement["revenue"].value == 105.0
    assert after.income_statement["revenue"].provenance.form == "10-K/A"


def test_tied_ranking_and_all_invalid(db: Database) -> None:
    facts_a = [
        make_fact(
            cik="0001999001",
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=100.0,
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            available_at=dt(2022, 2, 1),
            accession="a21",
            form="10-K",
            fiscal_year=2021,
            fiscal_period="FY",
        ),
        make_fact(
            cik="0001999001",
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=110.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a22",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    facts_b = [
        make_fact(
            cik="0001999002",
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=200.0,
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            available_at=dt(2022, 2, 1),
            accession="b21",
            form="10-K",
            fiscal_year=2021,
            fiscal_period="FY",
        ),
        make_fact(
            cik="0001999002",
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=220.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="b22",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts_a, ticker="AAA", cik="0001999001")
        _seed(conn, facts_b, ticker="BBB", cik="0001999002")
        ranking = rank_factors(
            tickers=["AAA", "BBB"],
            factor="revenue_growth",
            as_of=dt(2023, 3, 1),
            period="FY2022",
            service=FinancialsService(conn),
        )
        empty = rank_factors(
            tickers=["AAA", "BBB"],
            factor="fcf_yield",
            as_of=dt(2023, 3, 1),
            period="FY2022",
            service=FinancialsService(conn),
        )
    assert ranking.valid_count == 2
    assert ranking.rows[0].rank == ranking.rows[1].rank == 1
    assert ranking.rows[0].percentile == ranking.rows[1].percentile
    assert empty.valid_count == 0
    assert len(empty.excluded) == 2


def test_duration_mismatch_53_week_warning(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=100.0,
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),  # 364 days
            available_at=dt(2022, 2, 1),
            accession="fy21",
            form="10-K",
            fiscal_year=2021,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=110.0,
            start=date(2022, 1, 1),
            end=date(2023, 1, 14),  # ~378 days vs prior 364 (~14-day gap)
            available_at=dt(2023, 2, 1),
            accession="fy22",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        result = FactorEngine(conn).calculate(
            "AUDIT",
            "revenue_growth",
            "FY2022",
            as_of=dt(2023, 3, 1),
            persist=False,
        )
    assert result.valid
    assert "duration_mismatch_53_week" in result.warnings


def test_quality_score_missing_components_get_no_credit(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=100.0,
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            available_at=dt(2022, 2, 1),
            accession="a",
            form="10-K",
            fiscal_year=2021,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=120.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="b",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        result = FactorEngine(conn).calculate(
            "AUDIT",
            "basic_quality_score",
            "FY2022",
            as_of=dt(2023, 3, 1),
            persist=False,
        )
    assert result.valid
    assert result.inputs.get("revenue_growth") == pytest.approx(0.2)
    # Missing components must not inflate the score as successes.
    assert any(w.startswith("quality_missing:") for w in result.warnings)
    assert result.value == pytest.approx(1.0)  # only available positive growth component


def test_factor_provenance_and_idempotent_materialization(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=100.0,
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            available_at=dt(2022, 2, 1),
            accession="fy21",
            form="10-K",
            fiscal_year=2021,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=110.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="fy22",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="NetCashProvidedByUsedInOperatingActivities",
            value=40.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="fy22",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="PaymentsToAcquirePropertyPlantAndEquipment",
            value=-10.0,
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="fy22",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        ),
    ]
    as_of = dt(2023, 3, 1)
    with db.session() as conn:
        _seed(conn, facts)
        engine = FactorEngine(conn)
        first = engine.calculate("AUDIT", "free_cash_flow", "FY2022", as_of=as_of)
        second = engine.calculate("AUDIT", "free_cash_flow", "FY2022", as_of=as_of)
        count = conn.execute("SELECT COUNT(*) FROM factor_values").fetchone()
        stored = FactorRepository(conn).get_value(
            ticker="AUDIT",
            factor="free_cash_flow",
            period=parse_period("FY2022"),
            as_of=as_of,
        )
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=as_of)
    assert first.value == second.value == 30.0
    assert first.source_filings
    assert count is not None and int(count[0]) == 1
    assert stored == pytest.approx(30.0)
    fcf = snap.derived["free_cash_flow"]
    assert set(fcf.provenance.derivation_accessions) == {"fy22"}


def test_mixed_units_rejected() -> None:
    facts = [
        make_fact(
            cik="0001999001",
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=7.5,
            unit="USD/shares",
            start=date(2022, 1, 1),
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="x",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        )
    ]
    result = select_canonical_value(
        concept="revenue",
        period=parse_period("FY2022"),
        facts=facts,
        as_of=dt(2023, 3, 1),
    )
    assert result.value.value is None


def test_instant_balance_sheet_uses_period_end(db: Database) -> None:
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="Assets",
            value=999.0,
            end=date(2022, 12, 31),
            available_at=dt(2023, 2, 1),
            accession="a",
            form="10-K",
            fiscal_year=2022,
            fiscal_period="FY",
        )
    ]
    with db.session() as conn:
        _seed(conn, facts)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2022", as_of=dt(2023, 3, 1))
    assets = snap.balance_sheet["total_assets"]
    assert assets.value == 999.0
    assert assets.provenance.period_start is None
    assert assets.provenance.period_end == date(2022, 12, 31)


def test_10k_comparative_years_prefer_current_period_end(db: Database) -> None:
    """10-K comparative columns often share fy/fp; prefer the current year end."""
    cik = "0001999001"
    facts = [
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=100.0,
            start=date(2020, 9, 27),
            end=date(2021, 9, 25),
            available_at=dt(2023, 11, 3),
            accession="10k-2023",
            form="10-K",
            fiscal_year=2023,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=200.0,
            start=date(2021, 9, 26),
            end=date(2022, 9, 24),
            available_at=dt(2023, 11, 3),
            accession="10k-2023",
            form="10-K",
            fiscal_year=2023,
            fiscal_period="FY",
        ),
        make_fact(
            cik=cik,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value=300.0,
            start=date(2022, 9, 25),
            end=date(2023, 9, 30),
            available_at=dt(2023, 11, 3),
            accession="10k-2023",
            form="10-K",
            fiscal_year=2023,
            fiscal_period="FY",
        ),
    ]
    with db.session() as conn:
        _seed(conn, facts)
        snap = FinancialsService(conn).get_snapshot("AUDIT", "FY2023", as_of=dt(2023, 12, 1))
    assert snap.income_statement["revenue"].value == 300.0
    assert snap.income_statement["revenue"].provenance.period_end == date(2023, 9, 30)

    mapping = get_mapping("income_tax_expense")
    assert "IncomeTaxesPaidNet" not in mapping.candidates


def test_combined_cash_sti_not_mapped_as_cash() -> None:
    mapping = get_mapping("cash_and_equivalents")
    assert "CashCashEquivalentsAndShortTermInvestments" not in mapping.candidates
