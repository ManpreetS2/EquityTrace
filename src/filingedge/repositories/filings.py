"""Filing persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from filingedge.models import Filing


class FilingsRepository:
    """Read/write access to the filings table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def upsert_many(self, filings: list[Filing]) -> None:
        """Insert or update filings by accession number."""
        now = datetime.now(UTC)
        for filing in filings:
            self._conn.execute(
                """
                INSERT INTO filings (
                    accession_number, cik, form, filing_date, report_date,
                    acceptance_datetime, available_at, primary_document,
                    file_number, film_number, is_xbrl, is_inline_xbrl,
                    source_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (accession_number) DO UPDATE SET
                    cik = excluded.cik,
                    form = excluded.form,
                    filing_date = excluded.filing_date,
                    report_date = excluded.report_date,
                    acceptance_datetime = excluded.acceptance_datetime,
                    available_at = excluded.available_at,
                    primary_document = excluded.primary_document,
                    file_number = excluded.file_number,
                    film_number = excluded.film_number,
                    is_xbrl = excluded.is_xbrl,
                    is_inline_xbrl = excluded.is_inline_xbrl,
                    source_url = excluded.source_url,
                    updated_at = excluded.updated_at
                """,
                [
                    filing.accession_number,
                    filing.cik,
                    filing.form,
                    filing.filing_date,
                    filing.report_date,
                    filing.acceptance_datetime,
                    filing.available_at,
                    filing.primary_document,
                    filing.file_number,
                    filing.film_number,
                    filing.is_xbrl,
                    filing.is_inline_xbrl,
                    filing.source_url,
                    now,
                    now,
                ],
            )

    def list_for_ticker(
        self,
        ticker: str,
        *,
        form: str | None = None,
        limit: int = 20,
    ) -> list[Filing]:
        """List filings for a ticker, optionally filtered by form."""
        sql = """
            SELECT f.accession_number, f.cik, f.form, f.filing_date, f.report_date,
                   f.acceptance_datetime, f.available_at, f.primary_document,
                   f.file_number, f.film_number, f.is_xbrl, f.is_inline_xbrl, f.source_url
            FROM filings f
            INNER JOIN securities s ON s.cik = f.cik
            WHERE s.ticker = ?
        """
        params: list[object] = [ticker.strip().upper()]
        if form:
            sql += " AND upper(f.form) = upper(?)"
            params.append(form.strip())
        sql += " ORDER BY f.filing_date DESC, f.available_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_filing(row) for row in rows]

    def count(self) -> int:
        """Return the number of filings."""
        result = self._conn.execute("SELECT COUNT(*) FROM filings").fetchone()
        return int(result[0]) if result else 0


def _row_to_filing(row: tuple[object, ...]) -> Filing:
    from datetime import date, datetime

    filing_date = row[3]
    report_date = row[4]
    acceptance_datetime = row[5]
    available_at = row[6]
    if not isinstance(filing_date, date):
        raise TypeError(f"Expected filing_date date, got {type(filing_date)!r}")
    if report_date is not None and not isinstance(report_date, date):
        raise TypeError(f"Expected report_date date|None, got {type(report_date)!r}")
    if acceptance_datetime is not None and not isinstance(acceptance_datetime, datetime):
        raise TypeError(
            f"Expected acceptance_datetime datetime|None, got {type(acceptance_datetime)!r}"
        )
    if not isinstance(available_at, datetime):
        raise TypeError(f"Expected available_at datetime, got {type(available_at)!r}")

    return Filing(
        accession_number=str(row[0]),
        cik=str(row[1]),
        form=str(row[2]),
        filing_date=filing_date,
        report_date=report_date,
        acceptance_datetime=acceptance_datetime,
        available_at=available_at,
        primary_document=row[7] if row[7] is None else str(row[7]),
        file_number=row[8] if row[8] is None else str(row[8]),
        film_number=row[9] if row[9] is None else str(row[9]),
        is_xbrl=bool(row[10]),
        is_inline_xbrl=bool(row[11]),
        source_url=row[12] if row[12] is None else str(row[12]),
    )
