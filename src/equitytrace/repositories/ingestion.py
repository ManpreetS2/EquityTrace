"""Atomic company ingestion and ingestion-run tracking."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any

import duckdb

from equitytrace.database import Database
from equitytrace.models import (
    Filing,
    FinancialFact,
    IngestionRun,
    IngestResult,
    Issuer,
    ResolvedTicker,
    Security,
)
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.filings import FilingsRepository
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
from equitytrace.sec.tickers import resolve_ticker
from equitytrace.sec.validation import validate_payloads, validate_snapshot

logger = logging.getLogger(__name__)


class IngestionError(RuntimeError):
    """Raised when company ingestion fails."""


class IngestionOutcomeUnknown(IngestionError):
    """The database could not establish whether publication committed."""


_locks_guard = Lock()
_writer_locks: dict[Path, RLock] = {}


def _writer_lock(path: Path) -> RLock:
    # In-process ingestion ownership only. DuckDB owns inter-process file locks.
    with _locks_guard:
        return _writer_locks.setdefault(path.resolve(), RLock())


class IngestionRepository:
    """Coordinates SEC fetch, normalization, and atomic persistence."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def ingest_ticker(self, client: SecClient, ticker: str) -> IngestResult:
        """Prepare outside the transaction; publish one complete issuer atomically."""
        with _writer_lock(self._database.path):
            self.reconcile_interrupted_runs()
            return self._ingest_ticker(client, ticker)

    def _ingest_ticker(self, client: SecClient, ticker: str) -> IngestResult:
        started = time.perf_counter()
        started_at = datetime.now(UTC)
        run_id = uuid.uuid4().hex
        resolved = resolve_ticker(client, ticker)
        self._record_run_start(run_id, resolved, started_at)

        try:
            submissions = fetch_all_submissions(client, resolved.cik)
            company_facts = fetch_company_facts(client, resolved.cik)

            validate_payloads(submissions, company_facts, resolved.cik)
            issuer = normalize_issuer(submissions, cik=resolved.cik)
            securities = normalize_securities(
                submissions,
                resolved_ticker=resolved.ticker,
                resolved_exchange=resolved.exchange,
            )
            filings = normalize_filings(submissions)
            filing_lookup = {f.accession_number: f for f in filings}
            facts = normalize_company_facts(company_facts, filings_by_accession=filing_lookup)

            security_count = self._persist_snapshot(
                issuer=issuer,
                securities=securities,
                filings=filings,
                facts=facts,
                run_id=run_id,
            )
        except IngestionOutcomeUnknown:
            raise
        except Exception as exc:
            try:
                self._record_run_finish(
                    run_id,
                    status="failed",
                    finished_at=datetime.now(UTC),
                    error_message=str(exc),
                )
            except Exception as bookkeeping_error:
                exc.add_note(f"Failure bookkeeping also failed: {bookkeeping_error}")
            raise IngestionError(f"Ingestion failed for {ticker.upper()}: {exc}") from exc

        result = IngestResult(
            ticker=resolved.ticker,
            cik=resolved.cik,
            legal_name=issuer.legal_name,
            issuer_count=1,
            security_count=security_count,
            filing_count=len(filings),
            fact_count=len(facts),
            elapsed_seconds=time.perf_counter() - started,
        )
        return result

    def latest_successful_run(self) -> IngestionRun | None:
        """Return the most recent successful ingestion run, if any."""
        with self._database.session(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT run_id, ticker, cik, started_at, finished_at, status,
                       issuer_count, security_count, filing_count, fact_count, error_message
                FROM ingestion_runs
                WHERE status = 'success'
                ORDER BY finished_at DESC NULLS LAST
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        started_at = row[3]
        finished_at = row[4]
        if not isinstance(started_at, datetime):
            raise TypeError(f"Expected started_at datetime, got {type(started_at)!r}")
        if finished_at is not None and not isinstance(finished_at, datetime):
            raise TypeError(f"Expected finished_at datetime|None, got {type(finished_at)!r}")
        return IngestionRun(
            run_id=str(row[0]),
            ticker=str(row[1]),
            cik=str(row[2]),
            started_at=started_at,
            finished_at=finished_at,
            status=str(row[5]),
            issuer_count=_as_int(row[6]),
            security_count=_as_int(row[7]),
            filing_count=_as_int(row[8]),
            fact_count=_as_int(row[9]),
            error_message=row[10] if row[10] is None else str(row[10]),
        )

    def database_stats(self) -> dict[str, Any]:
        """Return high-level database counts and latest ingestion info."""
        with self._database.session(read_only=True) as conn:
            issuers = IssuersRepository(conn).count()
            securities = SecuritiesRepository(conn).count()
            filings = FilingsRepository(conn).count()
            facts = FactsRepository(conn).count()
        latest = self.latest_successful_run()
        return {
            "database_path": str(self._database.path),
            "issuer_count": issuers,
            "security_count": securities,
            "filing_count": filings,
            "fact_count": facts,
            "latest_successful_ingestion": latest,
        }

    def _persist_snapshot(
        self,
        *,
        issuer: Issuer,
        securities: list[Security],
        filings: list[Filing],
        facts: list[FinancialFact],
        run_id: str | None = None,
    ) -> int:
        """Own exactly one transaction. No provider work or whole-payload SQL rows."""
        coverage = validate_snapshot(
            issuer,
            securities,
            filings,
            facts,
            require_complete=run_id is not None,
        )
        commit_attempted = False
        security_count = 0
        try:
            with self._database.session() as conn:
                conn.execute("BEGIN TRANSACTION")
                try:
                    self._validate_retention(conn, issuer.cik, filings, coverage)
                    save_company_snapshot(
                        conn,
                        issuer=issuer,
                        securities=securities,
                        filings=filings,
                        facts=facts,
                    )
                    row = conn.execute(
                        "SELECT count(*) FROM securities WHERE cik = ?", [issuer.cik]
                    ).fetchone()
                    security_count = int(row[0]) if row else 0
                    if run_id is not None:
                        self._finish_on_connection(
                            conn,
                            run_id,
                            status="success",
                            finished_at=datetime.now(UTC),
                            issuer_count=1,
                            security_count=security_count,
                            filing_count=len(filings),
                            fact_count=len(facts),
                        )
                    commit_attempted = True
                    self._commit(conn)
                except BaseException as exc:
                    if not commit_attempted:
                        try:
                            conn.execute("ROLLBACK")
                        except Exception as rollback_error:
                            exc.add_note(f"Rollback also failed: {rollback_error}")
                    raise
        except Exception as exc:
            if commit_attempted:
                # The connection scope has exited before inspecting durable state.
                if run_id is None:
                    raise IngestionOutcomeUnknown("COMMIT outcome unknown without run ID") from exc
                try:
                    status = self.reconcile_run(run_id)
                except Exception as inspection_error:
                    exc.add_note(f"COMMIT reconciliation failed: {inspection_error}")
                    raise IngestionOutcomeUnknown(
                        f"Unresolved COMMIT outcome for run {run_id}; reconcile before retry"
                    ) from exc
                if status == "success":
                    logger.warning(
                        "Recovered committed ingestion %s after acknowledgement error", run_id
                    )
                    return security_count
                if status != "failed":
                    raise IngestionOutcomeUnknown(
                        f"Unresolved COMMIT outcome for run {run_id}: {status}"
                    ) from exc
            raise
        return security_count

    @staticmethod
    def _commit(conn: duckdb.DuckDBPyConnection) -> None:
        """Single publication boundary, also usable by the qualification harness."""
        conn.execute("COMMIT")

    @staticmethod
    def _validate_retention(
        conn: duckdb.DuckDBPyConnection,
        cik: str,
        filings: list[Filing],
        coverage: set[tuple[object, ...]],
    ) -> None:
        # These checks precede every mutation and share its transaction snapshot.
        accessions = {f.accession_number for f in filings}
        cursor = conn.execute("SELECT accession_number FROM filings WHERE cik = ?", [cik])
        while rows := cursor.fetchmany(2000):
            if any(str(row[0]) not in accessions for row in rows):
                raise ValueError("Incomplete replacement: stored filing history is missing")
        cursor = conn.execute(
            """SELECT taxonomy, concept, unit, start_date, end_date, accession_number,
                      form, fiscal_year, fiscal_period, frame
               FROM financial_facts WHERE cik = ?""",
            [cik],
        )
        while rows := cursor.fetchmany(2000):
            if any(row not in coverage for row in rows):
                raise ValueError("Incomplete replacement: stored fact history is missing")

    def reconcile_run(self, run_id: str) -> str:
        """Inspect a closed writer's run; never demote an acknowledged success.

        Call only after our writer scope closes, under the same in-process lock.
        An inspectable running row has no committed publication because success
        and the data would have committed together.
        """
        with _writer_lock(self._database.path), self._database.session() as conn:
            conn.execute(
                """UPDATE ingestion_runs SET status = 'failed', finished_at = ?,
                          error_message = 'Interrupted before successful publication'
                   WHERE run_id = ? AND status = 'running'""",
                [datetime.now(UTC), run_id],
            )
            row = conn.execute(
                "SELECT status FROM ingestion_runs WHERE run_id = ?", [run_id]
            ).fetchone()
            return str(row[0]) if row else "missing"

    def reconcile_interrupted_runs(self) -> None:
        """Recover stale running bookkeeping under exclusive ingestion ownership.

        This does not inspect, certify or repair historical financial data.
        """
        with _writer_lock(self._database.path), self._database.session() as conn:
            conn.execute(
                """UPDATE ingestion_runs SET status = 'failed', finished_at = ?,
                          error_message = 'Interrupted before successful publication'
                   WHERE status = 'running'""",
                [datetime.now(UTC)],
            )

    def _record_run_start(
        self,
        run_id: str,
        resolved: ResolvedTicker,
        started_at: datetime,
    ) -> None:
        with self._database.session() as conn:
            conn.execute(
                """
                INSERT INTO ingestion_runs (
                    run_id, ticker, cik, started_at, status
                ) VALUES (?, ?, ?, ?, 'running')
                """,
                [run_id, resolved.ticker, resolved.cik, started_at],
            )

    def _record_run_finish(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: datetime,
        issuer_count: int = 0,
        security_count: int = 0,
        filing_count: int = 0,
        fact_count: int = 0,
        error_message: str | None = None,
    ) -> None:
        with self._database.session() as conn:
            self._finish_on_connection(
                conn,
                run_id,
                status=status,
                finished_at=finished_at,
                issuer_count=issuer_count,
                security_count=security_count,
                filing_count=filing_count,
                fact_count=fact_count,
                error_message=error_message,
            )

    @staticmethod
    def _finish_on_connection(
        conn: duckdb.DuckDBPyConnection,
        run_id: str,
        *,
        status: str,
        finished_at: datetime,
        issuer_count: int = 0,
        security_count: int = 0,
        filing_count: int = 0,
        fact_count: int = 0,
        error_message: str | None = None,
    ) -> None:
        changed = conn.execute(
            """
                UPDATE ingestion_runs
                SET finished_at = ?,
                    status = ?,
                    issuer_count = ?,
                    security_count = ?,
                    filing_count = ?,
                    fact_count = ?,
                    error_message = ?
                WHERE run_id = ? AND status = 'running'
                RETURNING run_id
                """,
            [
                finished_at,
                status,
                issuer_count,
                security_count,
                filing_count,
                fact_count,
                error_message,
                run_id,
            ],
        ).fetchone()
        if status == "success" and changed is None:
            raise ValueError("Publication requires its own running ingestion record")


def save_company_snapshot(
    conn: duckdb.DuckDBPyConnection,
    *,
    issuer: Issuer,
    securities: list[Security],
    filings: list[Filing],
    facts: list[FinancialFact],
) -> None:
    """Persist a normalized company snapshot using an existing connection."""
    IssuersRepository(conn).upsert(issuer)
    SecuritiesRepository(conn).upsert_many(securities)
    FilingsRepository(conn).upsert_many(filings)
    FactsRepository(conn).upsert_many(facts)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Expected int-like value, got {type(value)!r}")
    return int(value)
