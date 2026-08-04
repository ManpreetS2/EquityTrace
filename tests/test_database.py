"""Tests for DuckDB schema, atomic ingestion, and idempotency."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from equitytrace.database import Database
from equitytrace.models import Filing, FinancialFact, Issuer, Security
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.filings import FilingsRepository
from equitytrace.repositories.ingestion import (
    IngestionError,
    IngestionRepository,
    save_company_snapshot,
)
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.securities import SecuritiesRepository
from equitytrace.sec.client import SecClient
from equitytrace.sec.company_facts import fetch_company_facts
from equitytrace.sec.normalization import (
    normalize_company_facts,
    normalize_filings,
    normalize_issuer,
    normalize_securities,
)
from equitytrace.sec.submissions import fetch_all_submissions


def test_schema_initialize_idempotent(db: Database) -> None:
    db.initialize()
    db.initialize()
    with db.session(read_only=True) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
    assert {
        "issuers",
        "securities",
        "filings",
        "financial_facts",
        "ingestion_runs",
    }.issubset(tables)


def test_two_tickers_one_issuer_persisted(db: Database) -> None:
    issuer = Issuer(cik="0000320193", legal_name="Apple Inc.", sic="3571")
    securities = [
        Security(ticker="AAPL", cik="0000320193", exchange="Nasdaq", is_primary=True),
        Security(ticker="AAPL.W", cik="0000320193", title="Apple Warrant", is_primary=False),
    ]
    with db.session() as conn:
        IssuersRepository(conn).upsert(issuer)
        SecuritiesRepository(conn).upsert_many(securities)

    with db.session(read_only=True) as conn:
        assert IssuersRepository(conn).count() == 1
        assert SecuritiesRepository(conn).count() == 2
        assert SecuritiesRepository(conn).resolve_cik("AAPL") == "0000320193"
        assert SecuritiesRepository(conn).resolve_cik("AAPL.W") == "0000320193"


def test_amended_filing_does_not_overwrite_original(db: Database) -> None:
    issuer = Issuer(cik="0000320193", legal_name="Apple Inc.")
    original = Filing(
        accession_number="0000320193-23-000106",
        cik="0000320193",
        form="10-K",
        filing_date=date(2023, 11, 3),
        acceptance_datetime=datetime(2023, 11, 3, 17, 0, 0, tzinfo=UTC),
        available_at=datetime(2023, 11, 3, 17, 0, 0, tzinfo=UTC),
    )
    amendment = Filing(
        accession_number="0000320193-23-000200",
        cik="0000320193",
        form="10-K/A",
        filing_date=date(2023, 11, 20),
        acceptance_datetime=datetime(2023, 11, 20, 12, 0, 0, tzinfo=UTC),
        available_at=datetime(2023, 11, 20, 12, 0, 0, tzinfo=UTC),
    )
    with db.session() as conn:
        save_company_snapshot(
            conn,
            issuer=issuer,
            securities=[Security(ticker="AAPL", cik="0000320193", is_primary=True)],
            filings=[original, amendment],
            facts=[],
        )

    with db.session(read_only=True) as conn:
        rows = FilingsRepository(conn).list_for_ticker("AAPL", limit=10)
        by_accn = {f.accession_number: f for f in rows}
        assert by_accn["0000320193-23-000106"].form == "10-K"
        assert by_accn["0000320193-23-000200"].form == "10-K/A"
        assert FilingsRepository(conn).count() == 2


def test_persist_failure_during_facts_is_repairable_by_reingest(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Large live Company Facts payloads cannot use a DuckDB multi-statement
    transaction without OOM, so a mid-persist failure may leave issuer rows.

    Re-ingestion must still repair and remain idempotent.
    """
    repo = IngestionRepository(db)
    issuer = Issuer(cik="0000320193", legal_name="Apple Inc.")
    securities = [Security(ticker="AAPL", cik="0000320193", is_primary=True)]
    facts = [
        FinancialFact(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="Assets",
            unit="USD",
            value=1.0,
            available_at=datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC),
            end_date=date(2023, 12, 30),
        ).with_fact_id()
    ]

    original_upsert = FactsRepository.upsert_many

    def exploding_upsert(self: FactsRepository, rows: list[FinancialFact]) -> None:
        if rows:
            raise RuntimeError("forced fact insert failure")
        original_upsert(self, rows)

    monkeypatch.setattr(FactsRepository, "upsert_many", exploding_upsert)

    with pytest.raises(RuntimeError, match="forced fact insert failure"):
        repo._persist_atomic(
            issuer=issuer,
            securities=securities,
            filings=[],
            facts=facts,
        )

    with db.session(read_only=True) as conn:
        assert IssuersRepository(conn).count() == 1
        assert SecuritiesRepository(conn).count() == 1
        assert FactsRepository(conn).count() == 0

    monkeypatch.setattr(FactsRepository, "upsert_many", original_upsert)
    repo._persist_atomic(
        issuer=issuer,
        securities=securities,
        filings=[],
        facts=facts,
    )
    with db.session(read_only=True) as conn:
        assert FactsRepository(conn).count() == 1


def test_ingest_ticker_idempotent(db: Database, sec_client: SecClient) -> None:
    repo = IngestionRepository(db)
    first = repo.ingest_ticker(sec_client, "AAPL")
    second = repo.ingest_ticker(sec_client, "AAPL")

    assert first.filing_count == second.filing_count
    assert first.fact_count == second.fact_count
    assert first.filing_count >= 5
    assert first.fact_count >= 5

    with db.session(read_only=True) as conn:
        assert IssuersRepository(conn).count() == 1
        assert SecuritiesRepository(conn).count() == 1
        assert FilingsRepository(conn).count() == first.filing_count
        assert FactsRepository(conn).count() == first.fact_count


def test_ingest_records_failure_without_partial_data(
    db: Database,
    sec_client: SecClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = IngestionRepository(db)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "equitytrace.repositories.ingestion.fetch_all_submissions",
        boom,
    )
    with pytest.raises(IngestionError, match="Ingestion failed"):
        repo.ingest_ticker(sec_client, "AAPL")

    with db.session(read_only=True) as conn:
        row = conn.execute(
            "SELECT status FROM ingestion_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert IssuersRepository(conn).count() == 0


def test_case_insensitive_fact_search(db: Database, sec_client: SecClient) -> None:
    IngestionRepository(db).ingest_ticker(sec_client, "AAPL")
    with db.session(read_only=True) as conn:
        rows = FactsRepository(conn).search("AAPL", concept="revenue", limit=50)
    assert rows
    assert all("revenue" in f.concept.lower() for f in rows)


def test_end_to_end_offline_normalize_and_store(db: Database, sec_client: SecClient) -> None:
    submissions = fetch_all_submissions(sec_client, "0000320193")
    facts_payload = fetch_company_facts(sec_client, "0000320193")
    issuer = normalize_issuer(submissions)
    securities = normalize_securities(submissions, resolved_ticker="AAPL")
    filings = normalize_filings(submissions)
    facts = normalize_company_facts(
        facts_payload,
        filings_by_accession={f.accession_number: f for f in filings},
    )
    with db.session() as conn:
        conn.execute("BEGIN TRANSACTION")
        save_company_snapshot(
            conn,
            issuer=issuer,
            securities=securities,
            filings=filings,
            facts=facts,
        )
        conn.execute("COMMIT")
        stored = FactsRepository(conn).search("AAPL", concept="Assets", limit=50)
    assert stored
    assert all(f.available_at.tzinfo is not None for f in stored)
