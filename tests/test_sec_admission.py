"""Admission preserves reported period identity instead of normalizing malformed starts away."""

import copy

import pytest

from equitytrace.repositories import ingestion


@pytest.mark.parametrize("previously_published", [False, True])
def test_invalid_reported_start_rejected_before_publication(
    db, sec_client, monkeypatch, previously_published
):
    repo = ingestion.IngestionRepository(db)
    if previously_published:
        repo.ingest_ticker(sec_client, "AAPL")
    with db.session() as conn:
        before = {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in ("issuers", "securities", "filings", "financial_facts")
        }
    fetch = ingestion.fetch_company_facts

    def malformed(*args):
        payload = copy.deepcopy(fetch(*args))
        for concepts in payload["facts"].values():
            for item in concepts.values():
                for observations in item["units"].values():
                    if observations:
                        observations[0]["start"] = "not-a-date"
                        return payload
        raise AssertionError("Fixture lacks observations")

    monkeypatch.setattr(ingestion, "fetch_company_facts", malformed)
    with pytest.raises(ingestion.IngestionError):
        ingestion.IngestionRepository(db).ingest_ticker(sec_client, "AAPL")
    with db.session() as conn:
        for table in ("issuers", "securities", "filings", "financial_facts"):
            assert (
                sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr) == before[table]
            )
        assert conn.execute(
            "SELECT status FROM ingestion_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone() == ("failed",)
