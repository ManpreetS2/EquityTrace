"""Additive v0.3b factor_values.market_input_json migration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from equitytrace.database import Database, initialize_database
from equitytrace.factors.engine import FactorEngine
from helpers.financial_fixtures import alpha_facts, dt, seed_company

V03_FACTOR_VALUES = """
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
    unavailable_reason VARCHAR,
    ranking_direction VARCHAR NOT NULL,
    run_id VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, factor_name, fiscal_year, fiscal_period, as_of)
);
"""


def test_fresh_db_has_market_input_json(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "fresh.duckdb")
    with db.session() as conn:
        cols = {
            str(r[0]).lower()
            for r in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'factor_values'
                """
            ).fetchall()
        }
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    assert "market_input_json" in cols
    assert "0.3.0-b" in versions


def test_v030_factor_values_upgrade_is_additive(tmp_path: Path) -> None:
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
        conn.execute("INSERT INTO schema_migrations VALUES ('0.3.0-a2', CURRENT_TIMESTAMP, 'v03a')")
        conn.execute(V03_FACTOR_VALUES)
        conn.execute(
            """
            INSERT INTO factor_values (
                ticker, cik, factor_name, fiscal_year, fiscal_period, as_of,
                value, valid, inputs_json, source_filings_json, warnings_json,
                unavailable_reason, ranking_direction, run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "ALPHA",
                "0001000001",
                "revenue_growth",
                2023,
                "FY",
                as_of,
                0.2,
                True,
                "{}",
                "[]",
                "[]",
                None,
                "higher_is_better",
                "run-old",
                as_of,
            ],
        )

    db = initialize_database(path)
    initialize_database(path)
    with db.session() as conn:
        cols = {
            str(r[0]).lower()
            for r in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'factor_values'
                """
            ).fetchall()
        }
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations WHERE version = '0.3.0-b'"
            ).fetchall()
        ]
        row = conn.execute(
            """
            SELECT value, valid, market_input_json FROM factor_values
            WHERE ticker = 'ALPHA' AND factor_name = 'revenue_growth'
            """
        ).fetchone()
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    assert "market_input_json" in cols
    assert versions == ["0.3.0-b"]
    assert row is not None
    assert row[0] == 0.2
    assert row[1] is True
    assert row[2] is None
    assert "factor_values" in tables


def test_new_factor_persistence_after_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.duckdb"
    legacy = Database(path)
    with legacy.session() as conn:
        conn.execute(V03_FACTOR_VALUES)
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                notes VARCHAR
            )
            """
        )
    db = initialize_database(path)
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        FactorEngine(conn).calculate(
            "ALPHA",
            "revenue_growth",
            "FY2023",
            as_of=dt(2023, 12, 1),
        )
        stored = conn.execute(
            """
            SELECT valid, market_input_json FROM factor_values
            WHERE ticker = 'ALPHA' AND factor_name = 'revenue_growth'
            """
        ).fetchone()
    assert stored is not None
    assert stored[0] is True
    assert stored[1] is None
