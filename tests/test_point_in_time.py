"""Point-in-time query tests (look-ahead bias prevention)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from equitytrace.database import Database
from equitytrace.models import FinancialFact, Issuer, Security
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.ingestion import IngestionRepository, save_company_snapshot
from equitytrace.sec.client import SecClient


def test_fact_not_visible_before_available_at(db: Database) -> None:
    """
    A fact filed/accepted on February 15 must not appear in a January 31 query,
    even if its report period ended earlier.
    """
    issuer = Issuer(cik="0000320193", legal_name="Apple Inc.")
    security = Security(ticker="AAPL", cik="0000320193", is_primary=True)
    fact = FinancialFact(
        cik="0000320193",
        taxonomy="us-gaap",
        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
        unit="USD",
        value=119575000000,
        start_date=date(2023, 10, 1),
        end_date=date(2023, 12, 30),
        filing_date=date(2024, 2, 15),
        acceptance_datetime=datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC),
        available_at=datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC),
        accession_number="0000320193-24-000050",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q1",
    ).with_fact_id()

    with db.session() as conn:
        save_company_snapshot(
            conn,
            issuer=issuer,
            securities=[security],
            filings=[],
            facts=[fact],
        )

    as_of_jan_31 = datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC)
    as_of_feb_15 = datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)
    as_of_feb_16 = datetime(2024, 2, 16, 0, 0, 0, tzinfo=UTC)

    with db.session(read_only=True) as conn:
        repo = FactsRepository(conn)
        before = repo.get_facts_as_of("AAPL", as_of_jan_31, concept="Revenue")
        on_release = repo.get_facts_as_of("AAPL", as_of_feb_15, concept="Revenue")
        after = repo.get_facts_as_of("AAPL", as_of_feb_16, concept="Revenue")

    assert before == []
    assert len(on_release) == 1
    assert on_release[0].value == 119575000000
    assert len(after) == 1
    # Report end date must not be treated as availability.
    assert all(f.available_at <= as_of_feb_16 for f in after)
    assert all(f.end_date == date(2023, 12, 30) for f in after)


def test_point_in_time_with_ingested_fixtures(db: Database, sec_client: SecClient) -> None:
    IngestionRepository(db).ingest_ticker(sec_client, "AAPL")

    # Before the 2024-02-15 10-Q acceptance, Q1 revenue must be absent.
    before = datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC)
    after = datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)

    with db.session(read_only=True) as conn:
        repo = FactsRepository(conn)
        jan_facts = repo.get_facts_as_of("AAPL", before, concept="RevenueFromContract")
        feb_facts = repo.get_facts_as_of("AAPL", after, concept="RevenueFromContract")

    jan_q1 = [f for f in jan_facts if f.end_date == date(2023, 12, 30) and f.unit == "USD"]
    feb_q1 = [f for f in feb_facts if f.end_date == date(2023, 12, 30) and f.unit == "USD"]
    assert jan_q1 == []
    assert len(feb_q1) == 1
    assert feb_q1[0].available_at == datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)

    # Older 10-K revenue should already be visible in January.
    with db.session(read_only=True) as conn:
        jan_all_revenue = FactsRepository(conn).get_facts_as_of(
            "AAPL",
            before,
            concept="RevenueFromContract",
        )
    assert any(f.end_date == date(2023, 9, 30) for f in jan_all_revenue)


def test_naive_as_of_treated_as_utc(db: Database) -> None:
    issuer = Issuer(cik="0000320193", legal_name="Apple Inc.")
    security = Security(ticker="AAPL", cik="0000320193", is_primary=True)
    fact = FinancialFact(
        cik="0000320193",
        taxonomy="us-gaap",
        concept="Assets",
        unit="USD",
        value=1.0,
        available_at=datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC),
        end_date=date(2023, 12, 30),
    ).with_fact_id()
    with db.session() as conn:
        save_company_snapshot(
            conn,
            issuer=issuer,
            securities=[security],
            filings=[],
            facts=[fact],
        )
        naive = datetime(2024, 2, 15, 21, 30, 15)  # naive
        rows = FactsRepository(conn).get_facts_as_of("AAPL", naive)
    assert len(rows) == 1
