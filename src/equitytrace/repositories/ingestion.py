"""Atomic company ingestion and ingestion-run tracking."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
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

logger = logging.getLogger(__name__)


class IngestionError(RuntimeError):
    """Raised when company ingestion fails."""


class IngestionRepository:
    """Coordinates SEC fetch, normalization, and atomic persistence."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def ingest_ticker(self, client: SecClient, ticker: str) -> IngestResult:
        """
        Resolve, fetch, normalize, and atomically store one company.

        If normalization or insertion fails, no partial issuer data is committed.
        """
        started = time.perf_counter()
        started_at = datetime.now(UTC)
        run_id = uuid.uuid4().hex
        resolved = resolve_ticker(client, ticker)
        self._record_run_start(run_id, resolved, started_at)

        try:
            submissions = fetch_all_submissions(client, resolved.cik)
            company_facts = fetch_company_facts(client, resolved.cik)

            issuer = normalize_issuer(submissions, cik=resolved.cik)
            securities = normalize_securities(
                submissions,
                resolved_ticker=resolved.ticker,
                resolved_exchange=resolved.exchange,
            )
            filings = normalize_filings(submissions)
            filing_lookup = {f.accession_number: f for f in filings}
            facts = normalize_company_facts(company_facts, filings_by_accession=filing_lookup)

            self._persist_atomic(
                issuer=issuer,
                securities=securities,
                filings=filings,
                facts=facts,
            )
        except Exception as exc:
            self._record_run_finish(
                run_id,
                status="failed",
                finished_at=datetime.now(UTC),
                error_message=str(exc),
            )
            raise IngestionError(f"Ingestion failed for {ticker.upper()}: {exc}") from exc

        finished_at = datetime.now(UTC)
        result = IngestResult(
            ticker=resolved.ticker,
            cik=resolved.cik,
            legal_name=issuer.legal_name,
            issuer_count=1,
            security_count=len(securities),
            filing_count=len(filings),
            fact_count=len(facts),
            elapsed_seconds=time.perf_counter() - started,
        )
        self._record_run_finish(
            run_id,
            status="success",
            finished_at=finished_at,
            issuer_count=result.issuer_count,
            security_count=result.security_count,
            filing_count=result.filing_count,
            fact_count=result.fact_count,
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

    def _persist_atomic(
        self,
        *,
        issuer: Issuer,
        securities: list[Security],
        filings: list[Filing],
        facts: list[FinancialFact],
    ) -> None:
        """
        Persist one company snapshot.

        Filings/facts for a single CIK use DELETE + bulk INSERT (idempotent).
        An explicit multi-statement DuckDB transaction is avoided because live
        Company Facts payloads (~20k+ facts) OOM under ``BEGIN`` + upsert on
        constrained hosts; re-ingestion remains safe and countable.
        """
        with self._database.session() as conn:
            IssuersRepository(conn).upsert(issuer)
            SecuritiesRepository(conn).upsert_many(securities)
            FilingsRepository(conn).upsert_many(filings)
            FactsRepository(conn).upsert_many(facts)

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
            conn.execute(
                """
                UPDATE ingestion_runs
                SET finished_at = ?,
                    status = ?,
                    issuer_count = ?,
                    security_count = ?,
                    filing_count = ?,
                    fact_count = ?,
                    error_message = ?
                WHERE run_id = ?
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
            )


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
