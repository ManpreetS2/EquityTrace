"""Issuer persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from filingedge.models import Issuer


class IssuersRepository:
    """Read/write access to the issuers table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def upsert(self, issuer: Issuer) -> None:
        """Insert or update an issuer row."""
        now = datetime.now(UTC)
        self._conn.execute(
            """
            INSERT INTO issuers (
                cik, legal_name, entity_type, sic, sic_description,
                fiscal_year_end, state_of_incorporation, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (cik) DO UPDATE SET
                legal_name = excluded.legal_name,
                entity_type = excluded.entity_type,
                sic = excluded.sic,
                sic_description = excluded.sic_description,
                fiscal_year_end = excluded.fiscal_year_end,
                state_of_incorporation = excluded.state_of_incorporation,
                updated_at = excluded.updated_at
            """,
            [
                issuer.cik,
                issuer.legal_name,
                issuer.entity_type,
                issuer.sic,
                issuer.sic_description,
                issuer.fiscal_year_end,
                issuer.state_of_incorporation,
                now,
                issuer.updated_at
                if issuer.updated_at.tzinfo
                else issuer.updated_at.replace(tzinfo=UTC),
            ],
        )

    def get_by_cik(self, cik: str) -> Issuer | None:
        """Fetch an issuer by CIK."""
        row = self._conn.execute(
            """
            SELECT cik, legal_name, entity_type, sic, sic_description,
                   fiscal_year_end, state_of_incorporation, updated_at
            FROM issuers WHERE cik = ?
            """,
            [cik],
        ).fetchone()
        if row is None:
            return None
        return Issuer(
            cik=row[0],
            legal_name=row[1],
            entity_type=row[2],
            sic=row[3],
            sic_description=row[4],
            fiscal_year_end=row[5],
            state_of_incorporation=row[6],
            updated_at=row[7],
        )

    def count(self) -> int:
        """Return the number of issuers."""
        result = self._conn.execute("SELECT COUNT(*) FROM issuers").fetchone()
        return int(result[0]) if result else 0
