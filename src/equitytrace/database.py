"""DuckDB connection helpers and schema initialization."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

# DuckDB currently requires the ``pytz`` package at runtime when reading
# TIMESTAMPTZ columns into Python, even though application code uses zoneinfo.
logger = logging.getLogger(__name__)

SCHEMA_SQL = """
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

CREATE INDEX IF NOT EXISTS idx_securities_cik ON securities(cik);
CREATE INDEX IF NOT EXISTS idx_securities_ticker ON securities(ticker);
CREATE INDEX IF NOT EXISTS idx_filings_cik ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_form ON filings(form);
CREATE INDEX IF NOT EXISTS idx_filings_available_at ON filings(available_at);
CREATE INDEX IF NOT EXISTS idx_facts_cik ON financial_facts(cik);
CREATE INDEX IF NOT EXISTS idx_facts_concept ON financial_facts(concept);
CREATE INDEX IF NOT EXISTS idx_facts_available_at ON financial_facts(available_at);
CREATE INDEX IF NOT EXISTS idx_facts_accession ON financial_facts(accession_number);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_finished ON ingestion_runs(finished_at);

-- v0.2 additive tables (idempotent; safe for existing v0.1 databases)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS factor_runs (
    run_id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    cik VARCHAR NOT NULL,
    factor_name VARCHAR NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS factor_values (
    ticker VARCHAR NOT NULL,
    cik VARCHAR NOT NULL,
    factor_name VARCHAR NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    value DOUBLE,
    valid BOOLEAN NOT NULL,
    inputs_json VARCHAR,
    source_filings_json VARCHAR,
    warnings_json VARCHAR,
    unavailable_reason VARCHAR,
    ranking_direction VARCHAR NOT NULL,
    run_id VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, factor_name, fiscal_year, fiscal_period, as_of)
);

CREATE INDEX IF NOT EXISTS idx_factor_values_factor ON factor_values(factor_name);
CREATE INDEX IF NOT EXISTS idx_factor_values_as_of ON factor_values(as_of);
CREATE INDEX IF NOT EXISTS idx_factor_runs_ticker ON factor_runs(ticker);
"""


class Database:
    """Thin wrapper around a DuckDB database file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        """Open a new DuckDB connection. Caller owns and must close it."""
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(self.path), read_only=read_only)
        # Reduce DuckDB peak memory during large Company Facts upserts.
        if not read_only:
            conn.execute("SET preserve_insertion_order=false")
            conn.execute("SET threads=2")
        return conn

    @contextmanager
    def session(self, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        """Context-managed DuckDB connection."""
        conn = self.connect(read_only=read_only)
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create tables and indexes idempotently."""
        with self.session() as conn:
            conn.execute(SCHEMA_SQL)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, notes)
                VALUES (?, ?)
                ON CONFLICT (version) DO NOTHING
                """,
                ["0.2.0", "Canonical statements and fundamental factors"],
            )
        logger.info("Initialized DuckDB schema at %s", self.path)


def initialize_database(path: Path) -> Database:
    """Create a Database instance and ensure the schema exists."""
    db = Database(path)
    db.initialize()
    return db
