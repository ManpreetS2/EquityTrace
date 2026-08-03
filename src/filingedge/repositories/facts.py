"""Financial fact persistence and point-in-time queries."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb

from filingedge.models import FinancialFact, compute_fact_id


class FactsRepository:
    """Read/write access to financial facts with point-in-time filtering."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def upsert_many(self, facts: list[FinancialFact]) -> None:
        """Insert or update facts using deterministic fact IDs."""
        now = datetime.now(UTC)
        for fact in facts:
            fact_id = fact.fact_id or compute_fact_id(fact)
            self._conn.execute(
                """
                INSERT INTO financial_facts (
                    fact_id, cik, taxonomy, concept, label, description, unit, value,
                    start_date, end_date, filing_date, acceptance_datetime, available_at,
                    accession_number, form, fiscal_year, fiscal_period, frame, source_url,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (fact_id) DO UPDATE SET
                    label = excluded.label,
                    description = excluded.description,
                    value = excluded.value,
                    filing_date = excluded.filing_date,
                    acceptance_datetime = excluded.acceptance_datetime,
                    available_at = excluded.available_at,
                    form = excluded.form,
                    fiscal_year = excluded.fiscal_year,
                    fiscal_period = excluded.fiscal_period,
                    frame = excluded.frame,
                    source_url = excluded.source_url,
                    updated_at = excluded.updated_at
                """,
                [
                    fact_id,
                    fact.cik,
                    fact.taxonomy,
                    fact.concept,
                    fact.label,
                    fact.description,
                    fact.unit,
                    fact.value,
                    fact.start_date,
                    fact.end_date,
                    fact.filing_date,
                    fact.acceptance_datetime,
                    fact.available_at,
                    fact.accession_number,
                    fact.form,
                    fact.fiscal_year,
                    fact.fiscal_period,
                    fact.frame,
                    fact.source_url,
                    now,
                    now,
                ],
            )

    def search(
        self,
        ticker: str,
        *,
        concept: str | None = None,
        limit: int = 20,
    ) -> list[FinancialFact]:
        """Search facts for a ticker with optional case-insensitive concept match."""
        sql = """
            SELECT f.fact_id, f.cik, f.taxonomy, f.concept, f.label, f.description, f.unit,
                   f.value, f.start_date, f.end_date, f.filing_date, f.acceptance_datetime,
                   f.available_at, f.accession_number, f.form, f.fiscal_year, f.fiscal_period,
                   f.frame, f.source_url
            FROM financial_facts f
            INNER JOIN securities s ON s.cik = f.cik
            WHERE s.ticker = ?
        """
        params: list[object] = [ticker.strip().upper()]
        if concept:
            sql += " AND lower(f.concept) LIKE lower(?)"
            params.append(f"%{concept.strip()}%")
        sql += " ORDER BY f.end_date DESC NULLS LAST, f.available_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_fact(row) for row in rows]

    def get_facts_as_of(
        self,
        ticker: str,
        as_of: datetime,
        *,
        concept: str | None = None,
        limit: int | None = None,
    ) -> list[FinancialFact]:
        """
        Return facts known as of a historical timestamp.

        Only facts with ``available_at <= as_of`` are returned. Report period
        end dates are never used as availability dates.
        """
        as_of_utc = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)

        sql = """
            SELECT f.fact_id, f.cik, f.taxonomy, f.concept, f.label, f.description, f.unit,
                   f.value, f.start_date, f.end_date, f.filing_date, f.acceptance_datetime,
                   f.available_at, f.accession_number, f.form, f.fiscal_year, f.fiscal_period,
                   f.frame, f.source_url
            FROM financial_facts f
            INNER JOIN securities s ON s.cik = f.cik
            WHERE s.ticker = ?
              AND f.available_at <= ?
        """
        params: list[object] = [ticker.strip().upper(), as_of_utc]
        if concept:
            sql += " AND lower(f.concept) LIKE lower(?)"
            params.append(f"%{concept.strip()}%")
        sql += " ORDER BY f.concept, f.end_date DESC NULLS LAST, f.available_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_fact(row) for row in rows]

    def count(self) -> int:
        """Return the number of financial facts."""
        result = self._conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()
        return int(result[0]) if result else 0


def _row_to_fact(row: tuple[object, ...]) -> FinancialFact:
    value = row[7]
    if not isinstance(value, (int, float)):
        raise TypeError(f"Expected numeric fact value, got {type(value)!r}")
    acceptance_datetime = row[11]
    available_at = row[12]
    if acceptance_datetime is not None and not isinstance(acceptance_datetime, datetime):
        raise TypeError(
            f"Expected acceptance_datetime datetime|None, got {type(acceptance_datetime)!r}"
        )
    if not isinstance(available_at, datetime):
        raise TypeError(f"Expected available_at datetime, got {type(available_at)!r}")

    fiscal_year: int | None = None
    if row[15] is not None:
        if isinstance(row[15], bool) or not isinstance(row[15], (int, float, str)):
            raise TypeError(f"Expected fiscal_year int-like, got {type(row[15])!r}")
        fiscal_year = int(row[15])

    return FinancialFact(
        fact_id=str(row[0]),
        cik=str(row[1]),
        taxonomy=str(row[2]),
        concept=str(row[3]),
        label=row[4] if row[4] is None else str(row[4]),
        description=row[5] if row[5] is None else str(row[5]),
        unit=str(row[6]),
        value=float(value),
        start_date=row[8] if isinstance(row[8], date) or row[8] is None else None,
        end_date=row[9] if isinstance(row[9], date) or row[9] is None else None,
        filing_date=row[10] if isinstance(row[10], date) or row[10] is None else None,
        acceptance_datetime=acceptance_datetime,
        available_at=available_at,
        accession_number=row[13] if row[13] is None else str(row[13]),
        form=row[14] if row[14] is None else str(row[14]),
        fiscal_year=fiscal_year,
        fiscal_period=row[16] if row[16] is None else str(row[16]),
        frame=row[17] if row[17] is None else str(row[17]),
        source_url=row[18] if row[18] is None else str(row[18]),
    )
