"""Backward-compatible schema migration tests for v0.1 databases."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from equitytrace.database import SCHEMA_SQL, Database, initialize_database
from equitytrace.models import FinancialFact, Issuer, Security
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.ingestion import save_company_snapshot

V01_SCHEMA = """
CREATE TABLE IF NOT EXISTS issuers (
    cik VARCHAR PRIMARY KEY,
    legal_name VARCHAR NOT NULL,
    entity_type VARCHAR,
    sic VARCHAR,
    sic_description VARCHAR,
    fiscal_year_end VARCHAR,
    state_of_incorporation VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS securities (
    ticker VARCHAR NOT NULL,
    cik VARCHAR NOT NULL,
    title VARCHAR,
    exchange VARCHAR,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ticker, cik),
    FOREIGN KEY (cik) REFERENCES issuers(cik)
);

CREATE TABLE IF NOT EXISTS filings (
    accession_number VARCHAR PRIMARY KEY,
    cik VARCHAR NOT NULL,
    form VARCHAR NOT NULL,
    filing_date DATE NOT NULL,
    report_date DATE,
    acceptance_datetime TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    primary_document VARCHAR,
    file_number VARCHAR,
    film_number VARCHAR,
    is_xbrl BOOLEAN NOT NULL DEFAULT FALSE,
    is_inline_xbrl BOOLEAN NOT NULL DEFAULT FALSE,
    source_url VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cik) REFERENCES issuers(cik)
);

CREATE TABLE IF NOT EXISTS financial_facts (
    fact_id VARCHAR PRIMARY KEY,
    cik VARCHAR NOT NULL,
    taxonomy VARCHAR NOT NULL,
    concept VARCHAR NOT NULL,
    label VARCHAR,
    description VARCHAR,
    unit VARCHAR NOT NULL,
    value DOUBLE NOT NULL,
    start_date DATE,
    end_date DATE,
    filing_date DATE,
    acceptance_datetime TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    accession_number VARCHAR,
    form VARCHAR,
    fiscal_year INTEGER,
    fiscal_period VARCHAR,
    frame VARCHAR,
    source_url VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cik) REFERENCES issuers(cik)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    cik VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    issuer_count INTEGER NOT NULL DEFAULT 0,
    security_count INTEGER NOT NULL DEFAULT 0,
    filing_count INTEGER NOT NULL DEFAULT 0,
    fact_count INTEGER NOT NULL DEFAULT 0,
    error_message VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def test_v01_database_migrates_successfully(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    legacy = Database(path)
    with legacy.session() as conn:
        conn.execute(V01_SCHEMA)
        save_company_snapshot(
            conn,
            issuer=Issuer(cik="0000320193", legal_name="Apple Inc."),
            securities=[Security(ticker="AAPL", cik="0000320193", is_primary=True)],
            filings=[],
            facts=[
                FinancialFact(
                    cik="0000320193",
                    taxonomy="us-gaap",
                    concept="Assets",
                    unit="USD",
                    value=1.0,
                    end_date=date(2023, 9, 30),
                    available_at=datetime(2023, 11, 3, tzinfo=UTC),
                    fiscal_year=2023,
                    fiscal_period="FY",
                    form="10-K",
                    accession_number="0000320193-23-000106",
                ).with_fact_id()
            ],
        )

    # Apply current schema (includes v0.2 additive tables).
    db = initialize_database(path)
    with db.session() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                """
            ).fetchall()
        }
        assert "factor_runs" in tables
        assert "factor_values" in tables
        assert "schema_migrations" in tables
        assert "financial_facts" in tables

        facts = FactsRepository(conn).search("AAPL", concept="Assets")
        assert len(facts) == 1
        assert facts[0].value == 1.0

        version = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = '0.2.0'"
        ).fetchone()
        assert version is not None

    # Idempotent re-initialize
    initialize_database(path)
    assert "CREATE TABLE IF NOT EXISTS factor_values" in SCHEMA_SQL
