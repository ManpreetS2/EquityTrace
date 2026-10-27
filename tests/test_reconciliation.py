"""Fail-closed reconciliation with disposable databases and contradictory query views."""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from equitytrace.models import ResolvedTicker
from equitytrace.repositories.ingestion import IngestionOutcomeUnknown, IngestionRepository
from equitytrace.transactions import transaction_active

TABLES = ("ingestion_runs", "issuers", "securities", "filings", "financial_facts")


def contents(db):
    with db.session() as conn:
        return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in TABLES}


@pytest.fixture
def published(db, sec_client):
    repo = IngestionRepository(db)
    repo.ingest_ticker(sec_client, "AAPL")
    with db.session() as conn:
        run = conn.execute("SELECT * FROM ingestion_runs").fetchone()
    return repo, run


def start(repo, run_id):
    repo._record_run_start(
        run_id,
        ResolvedTicker(ticker="AAPL", cik="0000320193", company_name="Apple"),
        datetime.now(UTC),
    )


def inject(db, monkeypatch, transform, *, update_result=None):
    session = db.session
    calls = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchmany(self, size):
            rows, self.rows = self.rows[:size], self.rows[size:]
            return rows

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def execute(self, sql, params=None):
            calls.append(sql)
            result = self.raw.execute(sql, params)
            if sql.startswith("UPDATE") and update_result is not None:
                return Result(update_result)
            if sql.startswith("SELECT *") and "WHERE" in sql:
                return Result(transform(sql, params, result.fetchall()))
            return result

    @contextmanager
    def wrapped(*args, **kwargs):
        with session(*args, **kwargs) as conn:
            yield Connection(conn)

    monkeypatch.setattr(db, "session", wrapped)
    return calls


def test_healthy_success_preserved(db, published):
    repo, run = published
    before = contents(db)
    assert repo.reconcile_run(run[0]) == "success"
    assert contents(db) == before


def test_healthy_running_idempotent(db, published):
    repo, _ = published
    start(repo, "interrupted")
    before = contents(db)
    assert repo.reconcile_run("interrupted") == "failed"
    after = contents(db)
    assert all(after[t] == before[t] for t in TABLES[1:])
    row = next(r for r in after["ingestion_runs"] if r[0] == "interrupted")
    assert row[5] == "failed" and row[4] is not None and row[6:10] == (0, 0, 0, 0)
    assert repo.reconcile_run("interrupted") == "failed"
    assert contents(db) == after


@pytest.mark.parametrize("field,value", [(5, "failed"), (2, "wrong"), (9, 999), (3, None)])
def test_run_contradictions_no_mutation(db, published, monkeypatch, field, value):
    repo, run = published
    before = contents(db)

    def wrong(sql, params, rows):
        if "ingestion_runs" in sql:
            row = list(rows[0])
            row[field] = value
            return [tuple(row)]
        return rows

    with monkeypatch.context() as patch:
        calls = inject(db, patch, wrong)
        with pytest.raises(IngestionOutcomeUnknown, match="Run views"):
            repo.reconcile_run(run[0])
    assert not any(s.startswith("UPDATE") for s in calls)
    assert contents(db) == before


@pytest.mark.parametrize("missing", [True, False])
def test_running_selective_success_or_missing_unknown(db, published, monkeypatch, missing):
    repo, _ = published
    start(repo, "interrupted")
    before = contents(db)

    def wrong(sql, params, rows):
        if "ingestion_runs" in sql:
            if missing:
                return []
            row = list(rows[0])
            row[5] = "success"
            return [tuple(row)]
        return rows

    with monkeypatch.context() as patch:
        inject(db, patch, wrong)
        with pytest.raises(IngestionOutcomeUnknown):
            repo.reconcile_run("interrupted")
    assert contents(db) == before


@pytest.mark.parametrize("table", TABLES[1:])
def test_publication_view_disagreement(db, published, monkeypatch, table):
    repo, run = published
    before = contents(db)
    with monkeypatch.context() as patch:
        calls = inject(db, patch, lambda sql, params, rows: [] if f"FROM {table} " in sql else rows)
        with pytest.raises(IngestionOutcomeUnknown, match="Publication views"):
            repo.reconcile_run(run[0])
    assert not any(s.startswith("UPDATE") for s in calls)
    assert contents(db) == before


def test_consistent_wrong_counts_unknown(db, published):
    repo, run = published
    with db.session() as conn:
        conn.execute("UPDATE ingestion_runs SET fact_count=fact_count+1 WHERE run_id=?", [run[0]])
    before = contents(db)
    with pytest.raises(IngestionOutcomeUnknown, match="counts"):
        repo.reconcile_run(run[0])
    assert contents(db) == before


def test_automatic_guard_rolls_back_prior_transitions(db, published, monkeypatch):
    repo, _ = published
    for run_id in ("a-interrupted", "z-contradictory"):
        start(repo, run_id)
    before = contents(db)

    def wrong(sql, params, rows):
        return [] if "ingestion_runs" in sql and params == ["z-contradictory"] else rows

    with monkeypatch.context() as patch:
        calls = inject(db, patch, wrong)
        with pytest.raises(IngestionOutcomeUnknown):
            repo.reconcile_interrupted_runs()
    assert any(s.startswith("UPDATE") for s in calls)
    assert "ROLLBACK" in calls and "COMMIT" not in calls
    assert contents(db) == before


def test_multiple_running_caller_transaction_preserved(db, published, monkeypatch):
    repo, _ = published
    for run_id in ("z-last", "a-first"):
        start(repo, run_id)
    order = []
    original = repo._reconcile_on_connection

    def observe(conn, run_id):
        order.append(run_id)
        return original(conn, run_id)

    monkeypatch.setattr(repo, "_reconcile_on_connection", observe)
    with db.session() as caller:
        caller.execute("BEGIN")
        calls = inject(db, monkeypatch, lambda sql, params, rows: rows)
        repo.reconcile_interrupted_runs()
        assert transaction_active(caller)
        caller.execute("ROLLBACK")
    assert order == ["a-first", "z-last"]
    assert calls.count("BEGIN TRANSACTION") == calls.count("COMMIT") == 1
    assert [r[5] for r in contents(db)["ingestion_runs"]].count("failed") == 2


def test_uncertain_commit_conflict_preserves_original(db, published, sec_client, monkeypatch):
    repo, _ = published
    original = RuntimeError("lost acknowledgement")

    def lost_ack(conn):
        conn.execute("COMMIT")
        raise original

    def wrong(sql, params, rows):
        if "ingestion_runs" in sql:
            row = list(rows[0])
            row[5] = "failed"
            return [tuple(row)]
        return rows

    monkeypatch.setattr(repo, "_commit", lost_ack)
    with monkeypatch.context() as patch:
        inject(db, patch, wrong)
        with pytest.raises(IngestionOutcomeUnknown) as caught:
            repo.ingest_ticker(sec_client, "AAPL")
    assert caught.value.__cause__ is original
    assert any("reconciliation failed" in note for note in original.__notes__)
    assert all(r[5] == "success" for r in contents(db)["ingestion_runs"])


def test_missing_run_unknown(db):
    with pytest.raises(IngestionOutcomeUnknown):
        IngestionRepository(db).reconcile_run("missing")


def test_unverified_conditional_update_rolls_back(db, published, monkeypatch):
    repo, _ = published
    start(repo, "interrupted")
    before = contents(db)
    with monkeypatch.context() as patch:
        inject(db, patch, lambda sql, params, rows: rows, update_result=[])
        with pytest.raises(IngestionOutcomeUnknown, match="transition"):
            repo.reconcile_run("interrupted")
    assert contents(db) == before


@pytest.mark.parametrize("table", TABLES[1:])
def test_same_count_different_content_rejected(db, published, monkeypatch, table):
    repo, run = published
    before = contents(db)

    def wrong(sql, params, rows):
        if f"FROM {table} " in sql:
            row = list(rows[0])
            # Last field is updated_at: a full-domain difference, with counts unchanged.
            row[-1] = None
            return [tuple(row), *rows[1:]]
        return rows

    with monkeypatch.context() as patch:
        inject(db, patch, wrong)
        with pytest.raises(IngestionOutcomeUnknown, match="Publication views"):
            repo.reconcile_run(run[0])
    assert contents(db) == before


def test_open_error_is_unknown(db, monkeypatch):
    @contextmanager
    def unavailable():
        raise OSError("unavailable database")
        yield

    monkeypatch.setattr(db, "session", unavailable)
    with pytest.raises(IngestionOutcomeUnknown) as caught:
        IngestionRepository(db).reconcile_run("unknown")
    assert isinstance(caught.value.__cause__, OSError)


def test_post_update_disagreement_rolls_back(db, published, monkeypatch):
    repo, _ = published
    start(repo, "interrupted")
    before = contents(db)

    def wrong(sql, params, rows):
        if "ingestion_runs" in sql and rows and rows[0][5] == "failed":
            row = list(rows[0])
            row[9] = 123
            return [tuple(row)]
        return rows

    with monkeypatch.context() as patch:
        calls = inject(db, patch, wrong)
        with pytest.raises(IngestionOutcomeUnknown):
            repo.reconcile_run("interrupted")
    assert any(s.startswith("UPDATE") for s in calls)
    assert "ROLLBACK" in calls and "COMMIT" not in calls
    assert contents(db) == before


def test_superseded_success_not_certified_or_demoted(db, published, sec_client):
    repo, run = published
    repo.ingest_ticker(sec_client, "AAPL")
    before = contents(db)
    with pytest.raises(IngestionOutcomeUnknown, match="superseded"):
        repo.reconcile_run(run[0])
    assert contents(db) == before


def test_automatic_enumeration_crosses_scan_batch_boundary(db, published):
    repo, _ = published
    with db.session() as conn:
        conn.execute("""INSERT INTO ingestion_runs(run_id,ticker,cik,started_at,status)
            SELECT 'a-history-' || range::VARCHAR,'AAPL','0000320193',CURRENT_TIMESTAMP,'failed'
            FROM range(501)""")
    start(repo, "z-interrupted")
    repo.reconcile_interrupted_runs()
    with db.session() as conn:
        assert conn.execute(
            "SELECT status FROM ingestion_runs WHERE run_id='z-interrupted'"
        ).fetchone() == ("failed",)


def test_failed_record_with_publication_counts_is_unknown(db, published):
    repo, run = published
    with db.session() as conn:
        conn.execute("UPDATE ingestion_runs SET status='failed' WHERE run_id=?", [run[0]])
    before = contents(db)
    with pytest.raises(IngestionOutcomeUnknown, match="Ambiguous failed"):
        repo.reconcile_run(run[0])
    assert contents(db) == before
