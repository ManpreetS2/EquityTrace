"""Additive 0.3.0-c portfolio table migration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from equitytrace.database import Database, initialize_database

PORTFOLIO_TABLES = {
    "portfolio_runs",
    "portfolio_rebalances",
    "portfolio_weight_transitions",
    "portfolio_equity",
}


def test_fresh_db_records_v03c_once(tmp_path: Path) -> None:
    path = tmp_path / "fresh.duckdb"
    db = initialize_database(path)
    initialize_database(path)
    with db.session() as conn:
        tables = {str(row[0]) for row in conn.execute("SHOW TABLES").fetchall()}
        versions = [
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]
    assert tables >= PORTFOLIO_TABLES
    assert versions.count("0.3.0-c") == 1


def test_upgrade_from_pre_v03c_preserves_factor_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    legacy = Database(path)
    as_of = datetime(2023, 12, 1, tzinfo=UTC)
    with legacy.session() as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                notes VARCHAR
            )
            """
        )
        conn.execute("INSERT INTO schema_migrations VALUES ('0.3.0-b', CURRENT_TIMESTAMP, 'v03b')")
        conn.execute(
            """
            CREATE TABLE factor_values (
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
                market_input_json VARCHAR,
                unavailable_reason VARCHAR,
                ranking_direction VARCHAR NOT NULL,
                run_id VARCHAR,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, factor_name, fiscal_year, fiscal_period, as_of)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO factor_values (
                ticker, cik, factor_name, fiscal_year, fiscal_period, as_of,
                value, valid, inputs_json, source_filings_json, warnings_json,
                market_input_json, unavailable_reason, ranking_direction, run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "ALPHA",
                "0001000001",
                "roa",
                2023,
                "FY",
                as_of,
                0.1,
                True,
                "{}",
                "[]",
                "[]",
                None,
                None,
                "higher_is_better",
                "abc",
                as_of,
            ],
        )
    initialize_database(path)
    with Database(path).session() as conn:
        tables = {str(row[0]) for row in conn.execute("SHOW TABLES").fetchall()}
        versions = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        count = conn.execute("SELECT COUNT(*) FROM factor_values").fetchone()
        conn.execute("SELECT run_id FROM portfolio_runs LIMIT 0")
    assert tables >= PORTFOLIO_TABLES
    assert "0.3.0-c" in versions
    assert count is not None and int(count[0]) == 1
