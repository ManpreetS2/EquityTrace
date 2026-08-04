"""Optional persistence for factor runs and values."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb

from equitytrace.factors.models import FactorResult
from equitytrace.financials.models import FinancialPeriod


class FactorRepository:
    """Idempotent storage for factor calculation outputs."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def record_run(
        self,
        *,
        run_id: str,
        ticker: str,
        cik: str,
        factor: str,
        period: FinancialPeriod,
        as_of: datetime,
        status: str,
    ) -> None:
        now = datetime.now(UTC)
        self._conn.execute(
            """
            INSERT INTO factor_runs (
                run_id, ticker, cik, factor_name, fiscal_year, fiscal_period,
                as_of, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                status = excluded.status
            """,
            [
                run_id,
                ticker,
                cik,
                factor,
                period.fiscal_year,
                period.fiscal_period,
                as_of,
                status,
                now,
            ],
        )

    def upsert_value(self, *, run_id: str, result: FactorResult) -> None:
        """
        Upsert a factor value keyed by ticker/factor/period/as_of.

        Repeated calculations with the same natural key overwrite the stored
        value, keeping materialization idempotent.
        """
        now = datetime.now(UTC)
        self._conn.execute(
            """
            INSERT INTO factor_values (
                ticker, cik, factor_name, fiscal_year, fiscal_period, as_of,
                value, valid, inputs_json, source_filings_json, warnings_json,
                unavailable_reason, ranking_direction, run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker, factor_name, fiscal_year, fiscal_period, as_of)
            DO UPDATE SET
                cik = excluded.cik,
                value = excluded.value,
                valid = excluded.valid,
                inputs_json = excluded.inputs_json,
                source_filings_json = excluded.source_filings_json,
                warnings_json = excluded.warnings_json,
                unavailable_reason = excluded.unavailable_reason,
                ranking_direction = excluded.ranking_direction,
                run_id = excluded.run_id,
                updated_at = excluded.updated_at
            """,
            [
                result.ticker,
                result.cik,
                result.factor,
                result.period.fiscal_year,
                result.period.fiscal_period,
                result.as_of,
                result.value,
                result.valid,
                json.dumps(result.inputs),
                json.dumps(list(result.source_filings)),
                json.dumps(list(result.warnings)),
                result.unavailable_reason,
                result.ranking_direction,
                run_id,
                now,
            ],
        )

    def get_value(
        self,
        *,
        ticker: str,
        factor: str,
        period: FinancialPeriod,
        as_of: datetime,
    ) -> float | None:
        row = self._conn.execute(
            """
            SELECT value
            FROM factor_values
            WHERE ticker = ?
              AND factor_name = ?
              AND fiscal_year = ?
              AND fiscal_period = ?
              AND as_of = ?
              AND valid = TRUE
            """,
            [
                ticker.strip().upper(),
                factor,
                period.fiscal_year,
                period.fiscal_period,
                as_of,
            ],
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])
