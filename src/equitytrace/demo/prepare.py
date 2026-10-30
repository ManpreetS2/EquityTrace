"""Safe, idempotent preparation of the dedicated synthetic demo database.

Rules enforced here:

* The demo database lives in its own directory next to a ``demo_manifest.json``
  marker. A file without that marker is never touched, even with ``overwrite``.
* A new database is built beside the target, verified against the in-memory
  dataset, and only then renamed into place. A failed build leaves any existing
  demo database exactly as it was and never leaves a trusted partial dataset.
* Re-preparing an already valid demo database changes nothing, so saved research
  runs survive restarts and repeated "Load demo" clicks.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import duckdb

from equitytrace.database import Database
from equitytrace.demo.data import (
    DATASET_ID,
    DATASET_VERSION,
    DemoDataset,
    SeriesBundle,
    build_dataset,
    digest_rows,
    row_bar,
    row_fact,
    row_filing,
    row_instrument,
    row_issuer,
    row_security,
)
from equitytrace.repositories.ingestion import save_company_snapshot
from equitytrace.repositories.market import MarketRepository

DB_NAME = "equitytrace_demo.duckdb"
MANIFEST_NAME = "demo_manifest.json"
MARKER_KIND = "equitytrace-synthetic-demo"


class DemoPreparationError(RuntimeError):
    """The demo database could not be prepared or is not safe to replace."""


@dataclass(frozen=True, slots=True)
class DemoPaths:
    directory: Path

    @property
    def database(self) -> Path:
        return self.directory / DB_NAME

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST_NAME


@dataclass(frozen=True, slots=True)
class PrepareOutcome:
    action: str  # "created" | "unchanged" | "replaced"
    digest: str
    paths: DemoPaths


def read_manifest(paths: DemoPaths) -> dict[str, Any] | None:
    """Return the manifest if it is a valid demo marker, else ``None``."""
    try:
        payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != MARKER_KIND:
        return None
    return payload


def database_rows(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Canonical rows read back from a database, matching ``dataset_rows``."""
    rows = [f"dataset|{DATASET_ID}|{DATASET_VERSION}"]
    rows.extend(
        row_issuer(str(r[0]), str(r[1]), None if r[2] is None else str(r[2]))
        for r in conn.execute("SELECT cik, legal_name, sic_description FROM issuers").fetchall()
    )
    rows.extend(
        row_security(str(r[0]), str(r[1]), bool(r[2]), None if r[3] is None else str(r[3]))
        for r in conn.execute("SELECT ticker, cik, is_primary, exchange FROM securities").fetchall()
    )
    rows.extend(
        row_filing(str(r[0]), str(r[1]), str(r[2]), r[3], r[4], r[5])
        for r in conn.execute(
            "SELECT accession_number, cik, form, filing_date, report_date, available_at "
            "FROM filings"
        ).fetchall()
    )
    rows.extend(
        row_fact(
            fact_id=str(r[0]),
            cik=str(r[1]),
            concept=str(r[2]),
            value=float(r[3]),
            start_date=r[4],
            end_date=r[5],
            accession=None if r[6] is None else str(r[6]),
            fiscal_year=None if r[7] is None else int(r[7]),
            fiscal_period=None if r[8] is None else str(r[8]),
            available_at=r[9],
            source_url=None if r[10] is None else str(r[10]),
        )
        for r in conn.execute(
            "SELECT fact_id, cik, concept, value, start_date, end_date, accession_number, "
            "fiscal_year, fiscal_period, available_at, source_url FROM financial_facts"
        ).fetchall()
    )
    instruments = conn.execute(
        "SELECT instrument_id, canonical_symbol, asset_type, issuer_cik, security_ticker "
        "FROM market_instruments"
    ).fetchall()
    for inst in instruments:
        rows.append(
            row_instrument(
                str(inst[1]),
                str(inst[2]),
                None if inst[3] is None else str(inst[3]),
                None if inst[4] is None else str(inst[4]),
            )
        )
        cursor = conn.execute(
            "SELECT trading_date, open, high, low, close, volume, available_at "
            "FROM daily_price_bars WHERE instrument_id = ?",
            [inst[0]],
        )
        rows.extend(
            row_bar(str(inst[1]), r[0], (r[1], r[2], r[3], r[4]), int(r[5]), r[6])
            for r in cursor.fetchall()
        )
    return rows


def database_digest(path: Path) -> str:
    """Digest of the demo content stored in ``path`` (read-only)."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        conn.execute("SET TimeZone='UTC'")
        return digest_rows(database_rows(conn))
    finally:
        conn.close()


def _populate(path: Path, dataset: DemoDataset) -> None:
    """Fill a brand-new scratch database.

    The scratch file is verified and only then renamed into place, so a failure at any point
    discards it entirely; transactions here are for memory, not for atomic publication.
    Committing per issuer and per price series keeps DuckDB's transaction-local memory small
    (one transaction over all ~5,700 bars peaked above 1 GiB; this stays near the baseline).
    """
    db = Database(path)
    db.initialize()
    with db.session() as conn:
        for company in dataset.companies:
            _in_transaction(
                conn,
                partial(
                    save_company_snapshot,
                    conn,
                    issuer=company.issuer,
                    securities=list(company.securities),
                    filings=list(company.filings),
                    facts=list(company.facts),
                ),
            )
        market = MarketRepository(conn)
        for series in dataset.series:
            _in_transaction(conn, partial(_store_series, market, series))


def _store_series(market: MarketRepository, series: SeriesBundle) -> None:
    instrument = market.get_or_create_instrument(
        series.symbol,
        asset_type=series.asset_type,
        security_ticker=series.symbol if series.issuer_cik else None,
        issuer_cik=series.issuer_cik,
        exchange="SYNTHETIC",
    )
    if instrument.instrument_id != series.bars[0].instrument_id:
        raise DemoPreparationError("Instrument identity does not match dataset.")
    market.upsert_price_bars(list(series.bars))


def _in_transaction(conn: duckdb.DuckDBPyConnection, work: Callable[[], object]) -> None:
    conn.execute("BEGIN TRANSACTION")
    try:
        work()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _write_manifest(paths: DemoPaths, digest: str, dataset: DemoDataset) -> None:
    payload = {
        "kind": MARKER_KIND,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "content_digest": digest,
        "synthetic": True,
        "tickers": list(dataset.tickers),
        "fact_count": dataset.fact_count,
        "bar_count": dataset.bar_count,
        "prepared_at": datetime.now(UTC).isoformat(),
    }
    temporary = paths.manifest.with_name(f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, paths.manifest)


def _remove_database_files(path: Path) -> None:
    for candidate in (path, path.with_name(path.name + ".wal")):
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()


def verify_prepared(paths: DemoPaths, expected_digest: str) -> str | None:
    """Return ``None`` when the stored demo is intact, else a human-readable problem."""
    manifest = read_manifest(paths)
    if manifest is None:
        return "No valid demo manifest is present."
    if manifest.get("dataset_version") != DATASET_VERSION:
        return "The stored demo was built from a different dataset version."
    if not paths.database.exists():
        return "The demo database file is missing."
    try:
        actual = database_digest(paths.database)
    except duckdb.Error as exc:
        return f"The demo database cannot be read: {exc}"
    if actual != expected_digest or manifest.get("content_digest") != expected_digest:
        return "The stored demo content does not match the expected synthetic dataset."
    return None


def prepare_demo(directory: Path, *, overwrite: bool = False) -> PrepareOutcome:
    """Create (or verify) the synthetic demo database in ``directory``."""
    paths = DemoPaths(Path(directory))
    dataset = build_dataset()
    expected = dataset.digest()

    problem = verify_prepared(paths, expected) if paths.database.exists() else None
    if paths.database.exists() and problem is None and not overwrite:
        return PrepareOutcome("unchanged", expected, paths)

    replacing = paths.database.exists() or paths.manifest.exists()
    if replacing:
        manifest = read_manifest(paths)
        if manifest is None:
            raise DemoPreparationError(
                f"{paths.database} exists but is not marked as an EquityTrace synthetic demo "
                "database. It will not be overwritten. Choose another demo directory."
            )
        if not overwrite:
            raise DemoPreparationError(
                f"{problem or 'The demo database needs to be rebuilt'} Rebuilding would "
                "discard saved demo runs; pass overwrite=True to confirm."
            )

    paths.directory.mkdir(parents=True, exist_ok=True)
    building = paths.directory / f".building-{uuid.uuid4().hex}.duckdb"
    try:
        _populate(building, dataset)
        built = database_digest(building)
        if built != expected:
            raise DemoPreparationError("Freshly built demo database failed verification.")
        with contextlib.suppress(FileNotFoundError):
            paths.database.with_name(paths.database.name + ".wal").unlink()
        os.replace(building, paths.database)
        _write_manifest(paths, expected, dataset)
    finally:
        _remove_database_files(building)
    return PrepareOutcome("replaced" if replacing else "created", expected, paths)
