"""Security persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from filingedge.models import Security


class SecuritiesRepository:
    """Read/write access to the securities table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def upsert_many(self, securities: list[Security]) -> None:
        """Insert or update multiple securities."""
        now = datetime.now(UTC)
        for security in securities:
            self._conn.execute(
                """
                INSERT INTO securities (
                    ticker, cik, title, exchange, is_primary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticker, cik) DO UPDATE SET
                    title = excluded.title,
                    exchange = excluded.exchange,
                    is_primary = excluded.is_primary,
                    updated_at = excluded.updated_at
                """,
                [
                    security.ticker,
                    security.cik,
                    security.title,
                    security.exchange,
                    security.is_primary,
                    now,
                    security.updated_at
                    if security.updated_at.tzinfo
                    else security.updated_at.replace(tzinfo=UTC),
                ],
            )

    def get_by_ticker(self, ticker: str) -> list[Security]:
        """Fetch securities matching a ticker symbol."""
        rows = self._conn.execute(
            """
            SELECT ticker, cik, title, exchange, is_primary, updated_at
            FROM securities
            WHERE ticker = ?
            ORDER BY is_primary DESC, cik
            """,
            [ticker.strip().upper()],
        ).fetchall()
        return [
            Security(
                ticker=row[0],
                cik=row[1],
                title=row[2],
                exchange=row[3],
                is_primary=bool(row[4]),
                updated_at=row[5],
            )
            for row in rows
        ]

    def resolve_cik(self, ticker: str) -> str | None:
        """Resolve a ticker to its primary CIK, if known."""
        row = self._conn.execute(
            """
            SELECT cik
            FROM securities
            WHERE ticker = ?
            ORDER BY is_primary DESC, cik
            LIMIT 1
            """,
            [ticker.strip().upper()],
        ).fetchone()
        return str(row[0]) if row else None

    def count(self) -> int:
        """Return the number of securities."""
        result = self._conn.execute("SELECT COUNT(*) FROM securities").fetchone()
        return int(result[0]) if result else 0
