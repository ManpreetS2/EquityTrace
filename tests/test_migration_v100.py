"""Upgrade of a v0.3.2-shaped database to the v1.0 schema: additive, repeatable, non-destructive."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from equitytrace.database import Database, initialize_database
from equitytrace.models import FinancialFact, Issuer, Security
from equitytrace.repositories.ingestion import save_company_snapshot

V100_TABLES = ("stock_split_events", "stock_split_coverage")
V100_INDEXES = (
    "idx_stock_split_events_instrument",
    "idx_stock_split_events_available",
    "idx_stock_split_coverage_instrument",
)
STAMP = datetime(2024, 3, 15, 21, 0, tzinfo=UTC)


def _fingerprint(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every non-schema-bookkeeping table, for before/after comparison."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        conn.execute("SET TimeZone='UTC'")
        tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name NOT IN ('schema_migrations') "
                "AND table_name NOT IN ('stock_split_events', 'stock_split_coverage') "
                "ORDER BY table_name"
            ).fetchall()
        ]
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in tables
        }
    finally:
        conn.close()


def _legacy_v032_database(path: Path) -> None:
    """A populated database as a v0.3.2 install would have left it (no v1.0 objects)."""
    db = initialize_database(path)
    fact = FinancialFact(
        cik="0000000042",
        taxonomy="us-gaap",
        concept="Assets",
        unit="USD",
        value=123.0,
        end_date=date(2023, 12, 31),
        filing_date=date(2024, 2, 1),
        acceptance_datetime=STAMP,
        available_at=STAMP,
        accession_number="0000000042-24-000001",
        form="10-K",
        fiscal_year=2023,
        fiscal_period="FY",
    ).with_fact_id()
    with db.session() as conn:
        save_company_snapshot(
            conn,
            issuer=Issuer(cik="0000000042", legal_name="Legacy Co"),
            securities=[Security(ticker="LEG", cik="0000000042", is_primary=True)],
            filings=[],
            facts=[fact],
        )
        for index in V100_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        for table in V100_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version = '1.0.0-a'")


def _objects(path: Path) -> tuple[set[str], set[str], list[str]]:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        indexes = {
            str(r[0]) for r in conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        }
        versions = [
            str(r[0]) for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]
        return tables, indexes, versions
    finally:
        conn.close()


def test_legacy_database_lacks_the_v100_objects_before_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    _legacy_v032_database(path)
    tables, indexes, versions = _objects(path)
    assert not set(V100_TABLES) & tables and not set(V100_INDEXES) & indexes
    assert "1.0.0-a" not in versions and "0.3.0-c" in versions


def test_upgrade_adds_only_new_objects_and_keeps_every_existing_row(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    _legacy_v032_database(path)
    before = _fingerprint(path)
    assert any(rows for rows in before.values())  # the legacy data is non-trivial

    Database(path).initialize()
    tables, indexes, versions = _objects(path)
    assert set(V100_TABLES) <= tables and set(V100_INDEXES) <= indexes
    assert versions.count("1.0.0-a") == 1
    assert _fingerprint(path) == before  # nothing deleted, rewritten or "repaired"

    # The new tables start empty: absence of events is never presented as certified coverage.
    conn = duckdb.connect(str(path), read_only=True)
    try:
        for table in V100_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    finally:
        conn.close()


def test_repeated_initialization_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    _legacy_v032_database(path)
    Database(path).initialize()
    snapshot = _fingerprint(path)
    objects = _objects(path)
    for _ in range(3):
        Database(path).initialize()
    assert _fingerprint(path) == snapshot
    assert _objects(path)[:2] == objects[:2]
    assert sorted(_objects(path)[2]) == sorted(objects[2])
    assert _objects(path)[2].count("1.0.0-a") == 1


def test_upgraded_database_reads_the_pre_upgrade_evidence_unchanged(tmp_path: Path) -> None:
    from equitytrace.financials.service import FinancialsService

    path = tmp_path / "legacy.duckdb"
    _legacy_v032_database(path)
    Database(path).initialize()
    with Database(path).session(read_only=True) as conn:
        snapshot = FinancialsService(conn).get_snapshot("LEG", "FY2023", as_of=STAMP)
        before_public = FinancialsService(conn).get_snapshot(
            "LEG", "FY2023", as_of=datetime(2024, 3, 15, 20, 59, tzinfo=UTC)
        )
    total_assets = snapshot.get_value("total_assets")
    assert total_assets is not None and total_assets.value == 123.0
    early = before_public.get_value("total_assets")
    assert early is not None and early.value is None  # availability still enforced


def test_a_demo_database_opens_and_reinitializes_without_data_loss(demo_dir: Path) -> None:
    path = demo_dir / "equitytrace_demo.duckdb"
    before = _fingerprint(path)
    Database(path).initialize()
    assert _fingerprint(path) == before
