"""Non-destructive recovery harness controls; no process termination is executed."""

from __future__ import annotations

import importlib
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("qualify_sec_recovery")


def test_default_plan_cannot_terminate(harness):
    assert harness.plan()["executed"] is False
    assert harness.plan()["cases"] == 6
    with pytest.raises(ValueError):
        harness.run(Path("never-created"))


@pytest.mark.parametrize("phase", ["before_commit", "after_ack", "pre_or_during_commit"])
def test_boundary_classification_requires_evidence(harness, tmp_path, phase):
    harness.prepare_case(tmp_path / "case", phase, "direct_index_first", count=3)
    directory = tmp_path / "case"
    harness.write_json(
        directory / "expected.json",
        {
            "run_id": "candidate",
            "case_id": json.loads((directory / "case.json").read_text())["case_id"],
        },
    )
    with pytest.raises(harness.RecoveryMismatch):
        harness.classify_phase(directory)
    harness.event(directory, "ready", "candidate")
    if phase == "before_commit":
        assert harness.classify_phase(directory) == ("proven_before_commit", ["old"], True)
        harness.event(directory, "commit_call_pending", "candidate")
        with pytest.raises(harness.RecoveryMismatch, match="advanced"):
            harness.classify_phase(directory)
    elif phase == "after_ack":
        with pytest.raises(harness.RecoveryMismatch, match="lacks"):
            harness.classify_phase(directory)
        harness.event(directory, "commit_call_pending", "candidate")
        harness.event(directory, "commit_ack", "candidate")
        # Ready must be written after the acknowledgement.
        with pytest.raises(harness.RecoveryMismatch, match="preceded"):
            harness.classify_phase(directory)
        harness.event(directory, "ready", "candidate")
        assert harness.classify_phase(directory) == ("proven_after_ack", ["new"], True)
    else:
        harness.event(directory, "commit_call_pending", "candidate")
        assert harness.classify_phase(directory) == (
            "ambiguous_commit_boundary_unacknowledged",
            ["old", "new"],
            True,
        )
        harness.event(directory, "commit_ack", "candidate")
        assert harness.classify_phase(directory) == ("acknowledged_commit_race", ["new"], False)


def test_full_state_first_reads_and_retries_normal_exit(harness, tmp_path):
    directory = tmp_path / "control"
    harness.prepare_case(directory, "after_ack", "direct_index_first", count=51)
    (directory / "continue").write_text("normal completion")
    harness.worker(directory)
    harness.verify_case(directory, normal_exit=True)
    result = json.loads((directory / "verified.json").read_text())
    assert result["recovered"] == "new"
    assert result["termination_executed"] is False
    harness.retry_case(directory)
    assert json.loads((directory / "retries.json").read_text())["status"] == "passed"
    observed = json.loads((directory / "reads-recovery/observed.json").read_text())
    assert observed["tables"]["financial_facts"]["rows"] == 68
    assert observed["probes"]["historical_class"]["rows"] == 1
    assert observed["probes"]["new_class"]["rows"] == 1
    assert observed["probes"]["pit_before"]["rows"] == 0
    assert observed["probes"]["pit_old_at"]["rows"] == 0
    assert observed["probes"]["pit_new_at"]["rows"] == 51
    first = json.loads(
        (directory / "reads-recovery/profiles/index-target_cik_first.json").read_text()
    )
    assert "WHERE cik = ?" in first["query_name"]
    assert first["rows_returned"] == 51


def test_controlled_pre_commit_unwind_preserves_old_state(harness, tmp_path, monkeypatch):
    class ControlledUnwind(BaseException):
        pass

    def unwind(_directory):
        raise ControlledUnwind("Caught in this test; no process or signal interruption")

    directory = tmp_path / "pre-commit-control"
    harness.prepare_case(directory, "before_commit", "direct_index_first", count=51)
    monkeypatch.setattr(harness, "hold", unwind)
    with pytest.raises(ControlledUnwind):
        harness.worker(directory)
    assert harness.classify_phase(directory) == ("proven_before_commit", ["old"], True)
    expected = json.loads((directory / "expected.json").read_text())
    assert harness.verify_first_reads(directory, expected, label="unwind") == "old"
    harness.write_json(
        directory / "verified.json",
        {
            "status": "passed",
            "recovered": "old",
            "termination_executed": False,
            "classification": "controlled_python_unwind_only",
        },
    )
    harness.retry_case(directory)


@pytest.mark.parametrize("damage", ["issuer", "security", "filing", "fact", "status", "counts"])
def test_corrupt_full_publication_is_not_certified(harness, tmp_path, damage):
    directory = tmp_path / "control"
    harness.prepare_case(directory, "after_ack", "direct_index_first", count=3)
    (directory / "continue").write_text("normal completion")
    harness.worker(directory)
    run_id = json.loads((directory / "expected.json").read_text())["run_id"]
    with harness.Database(directory / "synthetic.duckdb").session() as conn:
        sql, params = {
            "issuer": ("UPDATE issuers SET legal_name='wrong' WHERE cik=?", [harness.TARGET]),
            "security": ("UPDATE securities SET exchange='wrong' WHERE cik=?", [harness.TARGET]),
            "filing": ("UPDATE filings SET primary_document='wrong' WHERE cik=?", [harness.TARGET]),
            "fact": ("UPDATE financial_facts SET source_url='wrong' WHERE cik=?", [harness.OTHER]),
            "status": ("UPDATE ingestion_runs SET status='failed' WHERE run_id=?", [run_id]),
            "counts": ("UPDATE ingestion_runs SET fact_count=0 WHERE run_id=?", [run_id]),
        }[damage]
        conn.execute(sql, params)
    with pytest.raises(harness.RecoveryMismatch):
        harness.verify_case(directory, normal_exit=True)
    assert not (directory / "verified.json").exists()
    with pytest.raises(FileNotFoundError):
        harness.retry_case(directory)


def test_index_scan_disagreement_fails_closed(harness):
    checks = importlib.import_module("sec_recovery_checks")
    with pytest.raises(harness.RecoveryMismatch, match="disagree"):
        checks.compare_views({"rows": 0}, {"rows": 2501})


def test_disagreement_preserves_both_views_before_failure(harness, tmp_path, monkeypatch):
    checks = importlib.import_module("sec_recovery_checks")
    directory = tmp_path / "control"
    harness.prepare_case(directory, "after_ack", "direct_index_first", count=3)
    (directory / "continue").write_text("normal completion")
    harness.worker(directory)

    def contradict(indexed, scanned):
        raise harness.RecoveryMismatch("Injected view disagreement")

    monkeypatch.setattr(checks, "compare_views", contradict)
    with pytest.raises(harness.RecoveryMismatch, match="Injected"):
        harness.verify_case(directory, normal_exit=True)
    assert (directory / "reads-recovery/index-first.json").exists()
    assert (directory / "reads-recovery/scan-probes.json").exists()
    assert (directory / "reads-recovery/observed.json").exists()
    assert not (directory / "verified.json").exists()


def test_actual_index_plan_required(harness, tmp_path):
    checks = importlib.import_module("sec_recovery_checks")
    directory = tmp_path / "case"
    harness.prepare_case(directory, "before_commit", "direct_index_first", count=3)
    with harness.Database(directory / "synthetic.duckdb").session() as conn:
        conn.execute("SET index_scan_max_count=0")
        conn.execute("SET index_scan_percentage=0")
        conn.execute("PRAGMA enable_profiling='json'")
        with pytest.raises(harness.RecoveryMismatch, match="Index Scan absent"):
            checks.profiled_query(
                conn,
                "SELECT * FROM financial_facts WHERE cik=?",
                [harness.TARGET],
                tmp_path / "wrong-plan.json",
                index_table="financial_facts",
            )


def test_wait_timeout_does_not_kill(harness):
    class Process:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("read-only verifier", timeout)

        def kill(self):
            pytest.fail("Timeout may not terminate a verifier")

    with pytest.raises(subprocess.TimeoutExpired):
        harness.checked_wait(Process(), timeout=0)


def test_reconciliation_rejects_wrong_status_read(harness, db):
    """Diagnostic proof of missing defense, not a claim of real engine corruption."""
    calls = []

    class Result:
        def fetchmany(self, size):
            return [("failed",)]

    class Contradictory:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, sql, params=None):
            calls.append(sql)
            if sql.startswith("SELECT * FROM ingestion_runs WHERE run_id"):
                return Result()  # Inject wrong selective read; full stored row is success.
            return self.raw.execute(sql, params)

    with db.session() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs(run_id,ticker,cik,started_at,status) "
            "VALUES ('committed','SYNTH',?,CURRENT_TIMESTAMP,'success')",
            [harness.TARGET],
        )
    session = db.session

    @contextmanager
    def contradictory_session():
        with session() as conn:
            yield Contradictory(conn)

    db.session = contradictory_session
    try:
        from equitytrace.repositories.ingestion import IngestionOutcomeUnknown

        with pytest.raises(IngestionOutcomeUnknown):
            harness.IngestionRepository(db).reconcile_run("committed")
    finally:
        db.session = session
    with db.session() as conn:
        assert conn.execute("SELECT status FROM ingestion_runs").fetchone() == ("success",)
    assert not any(sql.startswith("UPDATE") for sql in calls)


def test_duplicate_marker_ordinals_rejected(harness, tmp_path):
    directory = tmp_path / "case"
    harness.prepare_case(directory, "after_ack", "direct_index_first", count=3)
    case = json.loads((directory / "case.json").read_text())
    harness.write_json(directory / "expected.json", {"case_id": case["case_id"], "run_id": "run"})
    for name in ("commit_call_pending", "commit_ack", "ready"):
        harness.event(directory, name, "run")
    ack = json.loads((directory / "commit_ack.json").read_text())
    ready = json.loads((directory / "ready.json").read_text())
    ready["ordinal"] = ack["ordinal"]
    harness.write_json(directory / "ready.json", ready)
    with pytest.raises(harness.RecoveryMismatch, match="distinct"):
        harness.classify_phase(directory)


def test_failure_manifest_preserved_without_termination(harness, tmp_path, monkeypatch):
    def failed_prepare(*args):
        raise RuntimeError("synthetic preparation failure")

    monkeypatch.setattr(harness, "prepare_case", failed_prepare)
    root = tmp_path / "new"
    with pytest.raises(RuntimeError, match="preparation"):
        harness.run(root, self_check=True)
    assert json.loads((root / "failure.json").read_text())["type"] == "RuntimeError"
    before = sorted(p.name for p in root.iterdir())
    with pytest.raises(FileExistsError):
        harness.run(root, self_check=True)
    assert sorted(p.name for p in root.iterdir()) == before
    assert (
        "src/equitytrace/sec/validation.py"
        in json.loads((root / "environment.json").read_text())["source_sha256"]
    )
