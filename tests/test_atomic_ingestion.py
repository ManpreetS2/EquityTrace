"""Atomic publication regressions on disposable offline databases."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from equitytrace.database import Database
from equitytrace.factors.engine import FactorEngine
from equitytrace.financials.service import FinancialsService
from equitytrace.models import Issuer, ResolvedTicker, Security
from equitytrace.repositories import ingestion
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.filings import FilingsRepository
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.securities import SecuritiesRepository
from equitytrace.transactions import read_snapshot, transaction_active
from helpers.financial_fixtures import alpha_facts, dt, seed_company

CIK = "0000320193"
TABLES = ("issuers", "securities", "filings", "financial_facts")


def state(db: Database, cik: str = CIK, *, timestamps: bool = True) -> dict:
    with db.session() as conn:
        result = {}
        for table in TABLES:
            columns = [
                r[1]
                for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()
                if timestamps or r[1] not in ("created_at", "updated_at")
            ]
            rows = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE cik = ?", [cik]
            ).fetchall()
            result[table] = sorted(rows, key=str)
        return result


def statuses(db: Database) -> list[str]:
    with db.session() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT status FROM ingestion_runs ORDER BY started_at"
            ).fetchall()
        ]


def change_evidence(monkeypatch):
    submissions = ingestion.fetch_all_submissions
    company_facts = ingestion.fetch_company_facts

    def new_submissions(*args):
        payload = copy.deepcopy(submissions(*args))
        payload["name"] = "New complete issuer metadata"
        payload["tickers"].append("AAPL.NEW")
        payload["exchanges"].append("Nasdaq")
        payload["filings"]["recent"]["primaryDocument"] = [
            "changed.htm" for _ in payload["filings"]["recent"]["accessionNumber"]
        ]
        return payload

    def new_facts(*args):
        payload = copy.deepcopy(company_facts(*args))
        for concepts in payload["facts"].values():
            for concept in concepts.values():
                for observations in concept["units"].values():
                    for obs in observations:
                        obs["val"] = float(obs["val"]) * 2
        return payload

    monkeypatch.setattr(ingestion, "fetch_all_submissions", new_submissions)
    monkeypatch.setattr(ingestion, "fetch_company_facts", new_facts)


@pytest.fixture
def published(db, sec_client):
    repo = ingestion.IngestionRepository(db)
    repo.ingest_ticker(sec_client, "AAPL")
    return repo


def test_changed_identical_and_security_history(db, sec_client, published, monkeypatch):
    with db.session() as conn:
        SecuritiesRepository(conn).upsert_many(
            [Security(ticker="AAPL.HIST", cik=CIK, title="Historical class")]
        )
        IssuersRepository(conn).upsert(Issuer(cik="2", legal_name="Unaffected issuer"))
        SecuritiesRepository(conn).upsert_many([Security(ticker="OTHER", cik="2")])
    other = state(db, "0000000002")
    old = state(db, timestamps=False)
    change_evidence(monkeypatch)
    result = published.ingest_ticker(sec_client, "AAPL")
    changed = state(db, timestamps=False)
    assert changed != old
    assert result.security_count == 3  # current, newly reported and retained historical
    with db.session() as conn:
        assert SecuritiesRepository(conn).resolve_cik("AAPL.HIST") == CIK
        assert IssuersRepository(conn).get_by_cik(CIK).legal_name == "New complete issuer metadata"
    published.ingest_ticker(sec_client, "AAPL")
    assert state(db, timestamps=False) == changed
    assert state(db, "0000000002") == other
    assert statuses(db) == ["success", "success", "success"]


@pytest.mark.parametrize(
    "repository,method",
    [
        (IssuersRepository, "upsert"),
        (SecuritiesRepository, "upsert_many"),
        (FilingsRepository, "upsert_many"),
        (FactsRepository, "upsert_many"),
    ],
)
def test_each_write_failure_retains_entire_snapshot(
    db, sec_client, published, monkeypatch, repository, method
):
    before = state(db)
    change_evidence(monkeypatch)
    original = getattr(repository, method)

    def fail_after_write(self, value):
        original(self, value)
        raise RuntimeError("controlled write failure")

    monkeypatch.setattr(repository, method, fail_after_write)
    with pytest.raises(ingestion.IngestionError, match="controlled write failure"):
        published.ingest_ticker(sec_client, "AAPL")
    assert state(db) == before  # includes all creation/update timestamps and provenance
    assert statuses(db) == ["success", "failed"]
    monkeypatch.setattr(repository, method, original)
    published.ingest_ticker(sec_client, "AAPL")
    assert state(db) != before


@pytest.mark.parametrize("point", ["fetch", "normalize", "validate"])
def test_pre_transaction_failure_retains_snapshot(db, sec_client, published, monkeypatch, point):
    before = state(db)
    target = {
        "fetch": "fetch_all_submissions",
        "normalize": "normalize_company_facts",
        "validate": "validate_snapshot",
    }[point]

    def fail(*args, **kwargs):
        raise ValueError("preparation failure")

    monkeypatch.setattr(ingestion, target, fail)
    with pytest.raises(ingestion.IngestionError, match="preparation failure"):
        published.ingest_ticker(sec_client, "AAPL")
    assert state(db) == before
    assert statuses(db) == ["success", "failed"]


def test_between_fact_batches_rollback(db, sec_client, published, monkeypatch):
    before = state(db)
    original_normalize = ingestion.normalize_company_facts
    original_init = FactsRepository.__init__
    batches = []

    def many_facts(*args, **kwargs):
        facts = original_normalize(*args, **kwargs)
        return facts + [
            facts[0].model_copy(update={"concept": f"SyntheticExtra{i}"}).with_fact_id()
            for i in range(2001)
        ]

    class Fault:
        def __init__(self, conn):
            self.conn = conn

        def executemany(self, sql, rows):
            batches.append(len(rows))
            if len(batches) == 2:
                raise RuntimeError("between fact batches")
            return self.conn.executemany(sql, rows)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    def inject(self, conn):
        original_init(self, Fault(conn))

    monkeypatch.setattr(ingestion, "normalize_company_facts", many_facts)
    monkeypatch.setattr(FactsRepository, "__init__", inject)
    with pytest.raises(ingestion.IngestionError, match="between fact batches"):
        published.ingest_ticker(sec_client, "AAPL")
    assert batches[0] == 2000 and len(batches) == 2
    assert state(db) == before
    assert statuses(db) == ["success", "failed"]


def test_success_status_and_data_share_commit(db, sec_client, published, monkeypatch):
    before = state(db)
    change_evidence(monkeypatch)
    commit = ingestion.IngestionRepository._commit

    def inspect_boundary(conn):
        assert (
            conn.execute(
                "SELECT status FROM ingestion_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
            == "success"
        )
        assert state(db) == before
        assert statuses(db) == ["success", "running"]
        commit(conn)

    monkeypatch.setattr(ingestion.IngestionRepository, "_commit", staticmethod(inspect_boundary))
    published.ingest_ticker(sec_client, "AAPL")
    assert state(db) != before
    assert statuses(db) == ["success", "success"]


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_reconciles(db, sec_client, published, monkeypatch, committed):
    before = state(db)
    change_evidence(monkeypatch)
    commit = ingestion.IngestionRepository._commit

    def lost_ack(conn):
        if committed:
            commit(conn)
        raise RuntimeError("lost COMMIT acknowledgement")

    monkeypatch.setattr(ingestion.IngestionRepository, "_commit", staticmethod(lost_ack))
    if committed:
        published.ingest_ticker(sec_client, "AAPL")
        assert state(db) != before
        assert statuses(db) == ["success", "success"]
        with db.session() as conn:
            run_id = conn.execute(
                "SELECT run_id FROM ingestion_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
        assert published.reconcile_run(run_id) == "success"
        published._record_run_finish(
            run_id, status="failed", finished_at=datetime.now(UTC), error_message="late error"
        )
        assert statuses(db) == ["success", "success"]
    else:
        with pytest.raises(ingestion.IngestionError, match="lost COMMIT"):
            published.ingest_ticker(sec_client, "AAPL")
        assert state(db) == before
        assert statuses(db) == ["success", "failed"]
    monkeypatch.setattr(ingestion.IngestionRepository, "_commit", staticmethod(commit))
    published.ingest_ticker(sec_client, "AAPL")


def test_uninspectable_commit_is_explicit_unknown(db, sec_client, published, monkeypatch):
    commit = ingestion.IngestionRepository._commit

    def lost_ack(conn):
        commit(conn)
        raise RuntimeError("lost acknowledgement")

    def cannot_inspect(run_id):
        raise OSError("database unavailable")

    monkeypatch.setattr(ingestion.IngestionRepository, "_commit", staticmethod(lost_ack))
    monkeypatch.setattr(published, "reconcile_run", cannot_inspect)
    with pytest.raises(ingestion.IngestionOutcomeUnknown, match="Unresolved COMMIT outcome"):
        published.ingest_ticker(sec_client, "AAPL")
    assert statuses(db) == ["success", "success"]


def test_failure_bookkeeping_preserves_original_cause(db, sec_client, published, monkeypatch):
    def broken_fetch(*args):
        raise ValueError("original evidence error")

    def broken_bookkeeping(*args, **kwargs):
        raise OSError("bookkeeping unavailable")

    monkeypatch.setattr(ingestion, "fetch_all_submissions", broken_fetch)
    monkeypatch.setattr(published, "_record_run_finish", broken_bookkeeping)
    with pytest.raises(ingestion.IngestionError, match="original evidence error") as error:
        published.ingest_ticker(sec_client, "AAPL")
    assert isinstance(error.value.__cause__, ValueError)
    assert "bookkeeping unavailable" in " ".join(error.value.__cause__.__notes__)


@pytest.mark.parametrize(
    "kind",
    [
        "mixed_fact",
        "mixed_security",
        "mixed_filing",
        "duplicate_fact",
        "conflicting_fact",
        "duplicate_filing",
        "duplicate_security",
        "empty_facts",
        "empty_filings",
        "empty_securities",
        "lost_fact_history",
        "lost_filing_history",
    ],
)
def test_invalid_typed_replacements_do_not_delete(db, sec_client, published, monkeypatch, kind):
    before = state(db)
    original = ingestion.validate_snapshot

    def altered(issuer, securities, filings, facts, **kwargs):
        if kind == "mixed_fact":
            facts[0] = facts[0].model_copy(update={"cik": "0000000002"})
        elif kind == "mixed_filing":
            filings[0] = filings[0].model_copy(update={"cik": "0000000002"})
        elif kind == "mixed_security":
            securities[0] = securities[0].model_copy(update={"cik": "0000000002"})
        elif kind == "duplicate_fact":
            facts.append(facts[0])
        elif kind == "conflicting_fact":
            facts.append(facts[0].model_copy(update={"value": 42.0}))
        elif kind == "duplicate_filing":
            filings.append(filings[0])
        elif kind == "duplicate_security":
            securities.append(securities[0])
        elif kind == "empty_facts":
            facts.clear()
        elif kind == "empty_filings":
            filings.clear()
        elif kind == "empty_securities":
            securities.clear()
        elif kind == "lost_fact_history":
            facts.pop(0)
        elif kind == "lost_filing_history":
            filings.pop(0)
        return original(issuer, securities, filings, facts, **kwargs)

    monkeypatch.setattr(ingestion, "validate_snapshot", altered)
    with pytest.raises(ingestion.IngestionError):
        published.ingest_ticker(sec_client, "AAPL")
    assert state(db) == before
    assert statuses(db) == ["success", "failed"]


@pytest.mark.parametrize("kind", ["empty", "missing_facts", "wrong_cik", "ragged", "bad_date"])
def test_bad_raw_payloads_are_not_published(db, sec_client, published, monkeypatch, kind):
    before = state(db)
    original = (
        ingestion.fetch_company_facts if kind != "ragged" else ingestion.fetch_all_submissions
    )

    def bad(*args):
        payload = copy.deepcopy(original(*args))
        if kind == "empty":
            return {}
        if kind == "missing_facts":
            payload["facts"] = {}
        if kind == "wrong_cik":
            payload["cik"] = 2
        if kind == "ragged":
            payload["filings"]["recent"]["filingDate"].pop()
        if kind == "bad_date":
            next(iter(next(iter(payload["facts"].values())).values()))["units"]["USD"][0]["end"] = (
                "bad"
            )
        return payload

    monkeypatch.setattr(
        ingestion, "fetch_all_submissions" if kind == "ragged" else "fetch_company_facts", bad
    )
    with pytest.raises(ingestion.IngestionError):
        published.ingest_ticker(sec_client, "AAPL")
    assert state(db) == before


def test_stale_running_reconciles_without_certifying_data(db, published, sec_client):
    published._record_run_start(
        "interrupted",
        ResolvedTicker(ticker="AAPL", cik=CIK, company_name="Apple"),
        datetime.now(UTC),
    )
    before = state(db)
    published.reconcile_interrupted_runs()
    assert state(db) == before
    assert statuses(db) == ["success", "failed"]
    published.ingest_ticker(sec_client, "AAPL")
    assert statuses(db) == ["success", "failed", "success"]


def test_financial_reader_pins_metadata_and_facts(db, sec_client, published, monkeypatch):
    with db.session() as conn:
        old = FinancialsService(conn).get_snapshot("AAPL", "FY2023", dt(2025, 1, 1))
    change_evidence(monkeypatch)
    resolve = SecuritiesRepository.resolve_cik
    refreshed = False

    def publish_between_statements(self, ticker):
        nonlocal refreshed
        value = resolve(self, ticker)
        if not refreshed:
            refreshed = True
            published.ingest_ticker(sec_client, "AAPL")
        return value

    monkeypatch.setattr(SecuritiesRepository, "resolve_cik", publish_between_statements)
    with db.session() as conn:
        observed = FinancialsService(conn).get_snapshot("AAPL", "FY2023", dt(2025, 1, 1))
    assert observed == old
    with db.session() as conn:
        new = FinancialsService(conn).get_snapshot("AAPL", "FY2023", dt(2025, 1, 1))
    assert new != old


def test_factor_current_and_prior_share_snapshot(db, monkeypatch):
    facts = alpha_facts()
    with db.session() as conn:
        seed_company(conn, ticker="ALPHA", cik="0001000001", legal_name="Alpha", facts=facts)
        expected = FactorEngine(conn).calculate(
            "ALPHA", "revenue-growth", "FY2023", as_of=dt(2023, 12, 1), persist=False
        )
    original = FinancialsService.get_snapshot
    refreshed = False

    def replace_after_first_period(self, *args, **kwargs):
        nonlocal refreshed
        result = original(self, *args, **kwargs)
        if not refreshed:
            refreshed = True
            changed = [
                f.model_copy(
                    update={"value": f.value * (3 if f.fiscal_year == 2022 else 2)}
                ).with_fact_id()
                for f in facts
            ]
            ingestion.IngestionRepository(db)._persist_snapshot(
                issuer=Issuer(cik="0001000001", legal_name="Alpha"),
                securities=[Security(cik="0001000001", ticker="ALPHA")],
                filings=[],
                facts=changed,
            )
        return result

    monkeypatch.setattr(FinancialsService, "get_snapshot", replace_after_first_period)
    with db.session() as conn:
        observed = FactorEngine(conn).calculate(
            "ALPHA", "revenue-growth", "FY2023", as_of=dt(2023, 12, 1), persist=False
        )
        assert not transaction_active(conn)
    assert observed == expected


def test_read_context_joins_and_preserves_callers_transaction(db):
    with db.session() as conn:
        assert not transaction_active(conn)
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE caller_marker(value INTEGER)")
        with read_snapshot(conn), read_snapshot(conn):
            assert transaction_active(conn)
        assert transaction_active(conn)
        conn.execute("ROLLBACK")
        assert not transaction_active(conn)
        assert not conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name='caller_marker'"
        ).fetchall()


def test_pit_provenance_boundary_after_replacement(db, sec_client, published, monkeypatch):
    change_evidence(monkeypatch)
    published.ingest_ticker(sec_client, "AAPL")
    with db.session() as conn:
        facts = FactsRepository(conn).get_facts_as_of("AAPL", dt(2025, 1, 1))
        chosen = max(facts, key=lambda f: f.available_at)
        before = FactsRepository(conn).get_facts_as_of(
            "AAPL", chosen.available_at - timedelta(microseconds=1)
        )
        at = FactsRepository(conn).get_facts_as_of("AAPL", chosen.available_at)
        assert chosen.fact_id not in {f.fact_id for f in before}
        assert chosen in at
        assert chosen.source_url and chosen.accession_number
        assert chosen.acceptance_datetime is not None


def test_existing_file_reopen_and_initialize_compatible(db, sec_client, published):
    before = state(db)
    reopened = Database(db.path)
    reopened.initialize()
    assert state(reopened) == before
    ingestion.IngestionRepository(reopened).ingest_ticker(sec_client, "AAPL")
    assert state(reopened, timestamps=False) == state(db, timestamps=False)
