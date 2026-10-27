"""Exact persistence equivalence and fail-closed bounded column insertion."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone

import duckdb
import pytest

from equitytrace.models import FinancialFact, Issuer
from equitytrace.repositories import facts as module
from equitytrace.repositories.facts import FactsRepository, _insert_fact_columns
from equitytrace.repositories.issuers import IssuersRepository

FIELDS = [
    "fact_id",
    "cik",
    "taxonomy",
    "concept",
    "label",
    "description",
    "unit",
    "value",
    "start_date",
    "end_date",
    "filing_date",
    "acceptance_datetime",
    "available_at",
    "accession_number",
    "form",
    "fiscal_year",
    "fiscal_period",
    "frame",
    "source_url",
]
NOW = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)


def evidence() -> list[FinancialFact]:
    offset = timezone(timedelta(hours=5, minutes=30))
    return [
        FinancialFact(
            fact_id="exact-id-'\u03b1-000001",
            cik="1",
            taxonomy="us-gaap",
            concept="ExactConcept",
            label="Unicode \u03b1; 'quoted'",
            description="Line one\nLine two\t" * 50,
            unit="USD/shares",
            value=-123456789.125,
            start_date=date(2024, 2, 29),
            end_date=date(2025, 12, 31),
            filing_date=date(2026, 1, 2),
            acceptance_datetime=datetime(2026, 1, 2, 10, 30, 0, 123456, tzinfo=offset),
            available_at=datetime(2026, 1, 2, 11, 0, 0, 654321, tzinfo=offset),
            accession_number="0000000001-26-000001",
            form="10-K/A",
            fiscal_year=2025,
            fiscal_period="FY",
            frame="CY2025",
            source_url="https://www.sec.gov/Archives/edgar/data/1/exact.htm?x=1&y=2",
        ),
        FinancialFact(
            fact_id="exact-id-null-fields",
            cik="1",
            taxonomy="dei",
            concept="NullOptionals",
            unit="shares",
            value=-0.0,
            available_at=NOW,
        ),
    ]


def stored_bytes(conn) -> bytes:
    def exact(value):
        if isinstance(value, float):
            return value.hex()  # Includes the sign of zero and exact double value.
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value

    rows = conn.execute("SELECT * FROM financial_facts ORDER BY fact_id").fetchall()
    return json.dumps([[exact(v) for v in row] for row in rows], ensure_ascii=False).encode()


def test_all_21_fields_match_previous_row_writer(db, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", Clock)
    facts = evidence()
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="1", legal_name="Exact"))
        # Independent row-wise VALUES oracle, matching the prior persisted column order.
        rows = [[getattr(f, field) for field in FIELDS] + [NOW, NOW] for f in facts]
        conn.executemany("INSERT INTO financial_facts VALUES (" + ",".join(["?"] * 21) + ")", rows)
        expected = stored_bytes(conn)
        conn.execute("BEGIN")
        FactsRepository(conn).upsert_many(facts)
        conn.execute("COMMIT")
        assert stored_bytes(conn) == expected
        for sql_type, actual in zip(
            [t for _, t in module._FACT_COLUMN_TYPES],
            conn.execute("DESCRIBE financial_facts").fetchall(),
            strict=True,
        ):
            assert (
                "TIMESTAMP WITH TIME ZONE" if sql_type == "TIMESTAMPTZ" else sql_type
            ) == actual[1]
        FactsRepository(conn).upsert_many([])
        _insert_fact_columns(conn, [[] for _ in range(21)])
        assert stored_bytes(conn) == expected


@pytest.mark.parametrize("kind", ["missing", "extra", "ragged", "oversized", "nonarray"])
def test_invalid_array_shape_never_executes_sql(kind):
    class NoSQL:
        def execute(self, *args):
            pytest.fail("Invalid arrays reached SQL")

    columns = [[] for _ in range(21)]
    if kind == "missing":
        columns.pop()
    elif kind == "extra":
        columns.append([])
    elif kind == "ragged":
        columns[7].append(1.0)
    elif kind == "oversized":
        columns = [[None] * 2001 for _ in range(21)]
    else:
        columns[7] = None
    with pytest.raises(ValueError, match="column arrays"):
        _insert_fact_columns(NoSQL(), columns)


@pytest.mark.parametrize("kind", ["duplicate", "invalid_double", "invalid_integer"])
def test_failed_set_insert_preserves_published_contents_on_rollback(db, kind):
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="1", legal_name="Exact"))
        FactsRepository(conn).upsert_many(evidence())
        before = stored_bytes(conn)
        conn.execute("BEGIN")
        with pytest.raises(duckdb.Error):
            if kind == "duplicate":
                FactsRepository(conn).upsert_many([evidence()[0]] * 2)
            else:
                row = [getattr(evidence()[0], f) for f in FIELDS] + [NOW, NOW]
                row[7 if kind == "invalid_double" else 15] = "not-a-number"
                _insert_fact_columns(conn, [[v] for v in row])
        conn.execute("ROLLBACK")
        assert stored_bytes(conn) == before
