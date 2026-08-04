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

-- v0.3a additive tables (idempotent; safe for existing v0.1/v0.2 databases)
--
-- Note: canonical_symbol is UNIQUE in v0.3a. This assumes one active display
-- ticker per instrument and does not model historical ticker reuse across
-- issuers. instrument_id remains the stable primary key; a future migration
-- may relax the symbol uniqueness once ticker-history workflows exist.
CREATE TABLE IF NOT EXISTS market_instruments (
    instrument_id VARCHAR PRIMARY KEY,
    canonical_symbol VARCHAR NOT NULL UNIQUE,
    asset_type VARCHAR NOT NULL,
    security_ticker VARCHAR,
    issuer_cik VARCHAR,
    exchange VARCHAR,
    mic_code VARCHAR,
    currency VARCHAR NOT NULL DEFAULT 'USD',
    exchange_timezone VARCHAR NOT NULL DEFAULT 'America/New_York',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL
);

-- mapping_id is the primary key so the same provider_symbol can be reused
-- historically by different instruments after a prior mapping expires.
CREATE TABLE IF NOT EXISTS market_symbol_mappings (
    mapping_id VARCHAR PRIMARY KEY,
    instrument_id VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    provider_symbol VARCHAR NOT NULL,
    valid_from DATE,
    valid_to DATE,
    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL
    -- Intentionally no FOREIGN KEY to market_instruments: DuckDB rejects parent-row
    -- UPDATEs while FK children exist, which blocks safe instrument enrichment.
);

CREATE TABLE IF NOT EXISTS daily_price_bars (
    instrument_id VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    trading_date DATE NOT NULL,
    adjustment_mode VARCHAR NOT NULL,
    open DECIMAL(18, 6) NOT NULL,
    high DECIMAL(18, 6) NOT NULL,
    low DECIMAL(18, 6) NOT NULL,
    close DECIMAL(18, 6) NOT NULL,
    volume BIGINT NOT NULL,
    currency VARCHAR NOT NULL DEFAULT 'USD',
    exchange_timezone VARCHAR NOT NULL DEFAULT 'America/New_York',
    available_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_metadata_json VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (instrument_id, provider, trading_date, adjustment_mode)
    -- No FK: see market_symbol_mappings note (DuckDB parent UPDATE limitation).
);

CREATE TABLE IF NOT EXISTS market_data_runs (
    run_id VARCHAR PRIMARY KEY,
    provider VARCHAR NOT NULL,
    instrument_id VARCHAR NOT NULL,
    requested_start_date DATE NOT NULL,
    requested_end_date DATE NOT NULL,
    adjustment_modes_json VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    raw_row_count INTEGER NOT NULL DEFAULT 0,
    inserted_row_count INTEGER NOT NULL DEFAULT 0,
    updated_row_count INTEGER NOT NULL DEFAULT 0,
    unchanged_row_count INTEGER NOT NULL DEFAULT 0,
    rejected_row_count INTEGER NOT NULL DEFAULT 0,
    error_summary VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    -- No FK: see market_symbol_mappings note (DuckDB parent UPDATE limitation).
);

CREATE INDEX IF NOT EXISTS idx_market_instruments_symbol ON market_instruments(canonical_symbol);
CREATE INDEX IF NOT EXISTS idx_market_instruments_cik ON market_instruments(issuer_cik);
CREATE INDEX IF NOT EXISTS idx_market_mappings_instrument ON market_symbol_mappings(instrument_id);
CREATE INDEX IF NOT EXISTS idx_market_mappings_provider_symbol
    ON market_symbol_mappings(provider, provider_symbol);
CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_price_bars(trading_date);
CREATE INDEX IF NOT EXISTS idx_daily_bars_available_at ON daily_price_bars(available_at);
CREATE INDEX IF NOT EXISTS idx_market_data_runs_instrument ON market_data_runs(instrument_id);
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
            _ensure_market_symbol_mapping_schema(conn)
            _ensure_market_child_tables_without_fk(conn)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, notes)
                VALUES (?, ?)
                ON CONFLICT (version) DO NOTHING
                """,
                ["0.2.0", "Canonical statements and fundamental factors"],
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, notes)
                VALUES (?, ?)
                ON CONFLICT (version) DO NOTHING
                """,
                [
                    "0.3.0-a",
                    "Market-data foundation: prices, instruments, market cap",
                ],
            )
            # Preserve legacy label if an earlier uncommitted draft used it.
            conn.execute(
                """
                INSERT INTO schema_migrations (version, notes)
                VALUES (?, ?)
                ON CONFLICT (version) DO NOTHING
                """,
                [
                    "0.3.0a",
                    "Legacy alias for 0.3.0-a market-data foundation",
                ],
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, notes)
                VALUES (?, ?)
                ON CONFLICT (version) DO NOTHING
                """,
                [
                    "0.3.0-a1",
                    "Drop market child FKs to allow instrument enrichment under DuckDB",
                ],
            )
        logger.info("Initialized DuckDB schema at %s", self.path)


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = ?
        """,
        [table],
    ).fetchall()
    return {str(r[0]).lower() for r in rows}


def _ensure_market_symbol_mapping_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Upgrade pre-mapping_id market_symbol_mappings without destroying data."""
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    if "market_symbol_mappings" not in tables:
        return
    cols = _table_columns(conn, "market_symbol_mappings")
    if "mapping_id" in cols:
        return
    conn.execute(
        """
        CREATE TABLE market_symbol_mappings_v03a (
            mapping_id VARCHAR PRIMARY KEY,
            instrument_id VARCHAR NOT NULL,
            provider VARCHAR NOT NULL,
            provider_symbol VARCHAR NOT NULL,
            valid_from DATE,
            valid_to DATE,
            is_primary BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_symbol_mappings_v03a (
            mapping_id, instrument_id, provider, provider_symbol,
            valid_from, valid_to, is_primary, created_at, updated_at
        )
        SELECT
            md5(provider || ':' || provider_symbol || ':' || instrument_id),
            instrument_id, provider, provider_symbol,
            valid_from, valid_to, is_primary, created_at, updated_at
        FROM market_symbol_mappings
        """
    )
    conn.execute("DROP TABLE market_symbol_mappings")
    conn.execute("ALTER TABLE market_symbol_mappings_v03a RENAME TO market_symbol_mappings")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_mappings_instrument
        ON market_symbol_mappings(instrument_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_mappings_provider_symbol
        ON market_symbol_mappings(provider, provider_symbol)
        """
    )


def _table_has_fk_to_instruments(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    rows = conn.execute(
        """
        SELECT constraint_name
        FROM information_schema.table_constraints
        WHERE table_name = ?
          AND constraint_type = 'FOREIGN KEY'
        """,
        [table],
    ).fetchall()
    return any("instrument_id" in str(r[0]).lower() for r in rows)


def _rebuild_table_without_fk(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    create_sql: str,
) -> None:
    tmp = f"{table}_nofk"
    conn.execute(create_sql)
    conn.execute(f"INSERT INTO {tmp} SELECT * FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _ensure_market_child_tables_without_fk(conn: duckdb.DuckDBPyConnection) -> None:
    """Rebuild market child tables without FKs so instrument enrichment can UPDATE.

    DuckDB rejects UPDATEs to parent rows that are referenced by foreign keys.
    Referential integrity for these tables is enforced in application code.
    """
    tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
    if "market_symbol_mappings" in tables and _table_has_fk_to_instruments(
        conn, "market_symbol_mappings"
    ):
        _rebuild_table_without_fk(
            conn,
            table="market_symbol_mappings",
            create_sql="""
            CREATE TABLE market_symbol_mappings_nofk (
                mapping_id VARCHAR PRIMARY KEY,
                instrument_id VARCHAR NOT NULL,
                provider VARCHAR NOT NULL,
                provider_symbol VARCHAR NOT NULL,
                valid_from DATE,
                valid_to DATE,
                is_primary BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_mappings_instrument
            ON market_symbol_mappings(instrument_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_mappings_provider_symbol
            ON market_symbol_mappings(provider, provider_symbol)
            """
        )
    if "daily_price_bars" in tables and _table_has_fk_to_instruments(conn, "daily_price_bars"):
        _rebuild_table_without_fk(
            conn,
            table="daily_price_bars",
            create_sql="""
            CREATE TABLE daily_price_bars_nofk (
                instrument_id VARCHAR NOT NULL,
                provider VARCHAR NOT NULL,
                trading_date DATE NOT NULL,
                adjustment_mode VARCHAR NOT NULL,
                open DECIMAL(18, 6) NOT NULL,
                high DECIMAL(18, 6) NOT NULL,
                low DECIMAL(18, 6) NOT NULL,
                close DECIMAL(18, 6) NOT NULL,
                volume BIGINT NOT NULL,
                currency VARCHAR NOT NULL DEFAULT 'USD',
                exchange_timezone VARCHAR NOT NULL DEFAULT 'America/New_York',
                available_at TIMESTAMPTZ NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL,
                source_metadata_json VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (instrument_id, provider, trading_date, adjustment_mode)
            )
            """,
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_price_bars(trading_date)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_daily_bars_available_at
            ON daily_price_bars(available_at)
            """
        )
    if "market_data_runs" in tables and _table_has_fk_to_instruments(conn, "market_data_runs"):
        _rebuild_table_without_fk(
            conn,
            table="market_data_runs",
            create_sql="""
            CREATE TABLE market_data_runs_nofk (
                run_id VARCHAR PRIMARY KEY,
                provider VARCHAR NOT NULL,
                instrument_id VARCHAR NOT NULL,
                requested_start_date DATE NOT NULL,
                requested_end_date DATE NOT NULL,
                adjustment_modes_json VARCHAR NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ,
                status VARCHAR NOT NULL,
                raw_row_count INTEGER NOT NULL DEFAULT 0,
                inserted_row_count INTEGER NOT NULL DEFAULT 0,
                updated_row_count INTEGER NOT NULL DEFAULT 0,
                unchanged_row_count INTEGER NOT NULL DEFAULT 0,
                rejected_row_count INTEGER NOT NULL DEFAULT 0,
                error_summary VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_data_runs_instrument
            ON market_data_runs(instrument_id)
            """
        )


def initialize_database(path: Path) -> Database:
    """Create a Database instance and ensure the schema exists."""
    db = Database(path)
    db.initialize()
    return db
