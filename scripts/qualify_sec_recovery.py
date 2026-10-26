"""Prepared deterministic crash qualification; process termination requires separate approval.

Default --plan is read-only. --execute-authorized permits killing disposable
worker processes and MUST NOT be used without separate user authorization.
The during-COMMIT arm is timing-sensitive: an observed acknowledgement before
termination invalidates that arm instead of being misreported as a passing crash.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_ingestion_synthetic import fact_digest, make_payloads

from equitytrace.database import Database
from equitytrace.models import ResolvedTicker
from equitytrace.repositories import ingestion

PHASES = ("before_commit", "during_commit", "after_ack")


def publish(db, scale, boundary=None):
    payloads = make_payloads(2501, scale)
    with (
        patch.object(
            ingestion,
            "resolve_ticker",
            return_value=ResolvedTicker(ticker="SYNTH", cik="0000999999", company_name="Synthetic"),
        ),
        patch.object(ingestion, "fetch_all_submissions", side_effect=lambda *_: payloads[0]),
        patch.object(ingestion, "fetch_company_facts", side_effect=lambda *_: payloads[1]),
    ):
        if boundary is None:
            ingestion.IngestionRepository(db).ingest_ticker(None, "SYNTH")
        else:
            with patch.object(ingestion.IngestionRepository, "_commit", staticmethod(boundary)):
                ingestion.IngestionRepository(db).ingest_ticker(None, "SYNTH")


def worker(directory, phase):
    db = Database(directory / "synthetic.duckdb")

    def signal(name):
        (directory / name).write_text(name)

    def wait():
        # This worker is deliberately held at an exact application boundary.
        while not (directory / "continue").exists():
            time.sleep(0.02)

    def boundary(conn):
        if phase == "before_commit":
            signal("ready")
            wait()
        if phase == "during_commit":
            signal("ready")
            wait()
            signal("commit_enter")
        conn.execute("COMMIT")
        signal("commit_ack")
        if phase == "after_ack":
            signal("ready")
            wait()

    publish(db, 2.0, boundary)


def verify(directory, phase):
    db = Database(directory / "synthetic.duckdb")
    with db.session() as conn:
        conn.execute("SET TimeZone='UTC'")
        observed = fact_digest(conn)
        # Full-domain scan supplies the oracle. No indexes are dropped/rebuilt.
        sample = conn.execute(
            "SELECT fact_id, value FROM financial_facts ORDER BY fact_id LIMIT 1"
        ).fetchone()
        plan = conn.execute(
            "EXPLAIN ANALYZE SELECT value FROM financial_facts WHERE fact_id = ?", [sample[0]]
        ).fetchall()
        assert "Index Scan" in str(plan), "Indexed path was not exercised"
        indexed = conn.execute(
            "SELECT value FROM financial_facts WHERE fact_id = ?", [sample[0]]
        ).fetchall()
        conn.execute("SET index_scan_max_count=0")
        conn.execute("SET index_scan_percentage=0")
        scan = conn.execute(
            "SELECT value FROM financial_facts WHERE fact_id = ?", [sample[0]]
        ).fetchall()
        assert indexed == scan == [(sample[1],)]
        assert fact_digest(conn) == observed
    expected = json.loads((directory / "expected.json").read_text())
    assert list(observed) in expected.values(), "Partial/unrecognized recovered snapshot"
    if phase == "after_ack":
        assert list(observed) == expected["new"], "Acknowledged publication was not durable"
    # Reconcile the interrupted run through the normal public API, retry the
    # recovered content identically, then change it. Validate actual content.
    scale = 2.0 if list(observed) == expected["new"] else 1.0
    publish(db, scale)
    with db.session() as conn:
        conn.execute("SET TimeZone='UTC'")
        assert fact_digest(conn) == observed
    publish(db, 2.0)
    with db.session() as conn:
        conn.execute("SET TimeZone='UTC'")
        assert list(fact_digest(conn)) == expected["new"]
        assert (
            conn.execute("SELECT count(*) FROM ingestion_runs WHERE status='running'").fetchone()[0]
            == 0
        )
    (directory / "verified.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "recovered": observed,
                "indexed_agreement": True,
                "identical_and_changed_retries": True,
                "status": "passed",
            },
            indent=2,
        )
    )


def await_file(path, process, timeout=60):
    until = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            raise RuntimeError(f"Worker exited early: {process.returncode}")
        if time.monotonic() > until:
            raise TimeoutError(str(path))
        time.sleep(0.01)


def run_authorized(root):
    root.mkdir(parents=True, exist_ok=False)
    for phase in PHASES:
        directory = root / phase
        directory.mkdir()
        db = Database(directory / "synthetic.duckdb")
        db.initialize()
        publish(db, 1.0)
        with db.session() as conn:
            conn.execute("SET TimeZone='UTC'")
            old = fact_digest(conn)
        publish(db, 2.0)
        with db.session() as conn:
            conn.execute("SET TimeZone='UTC'")
            new = fact_digest(conn)
        publish(db, 1.0)
        (directory / "expected.json").write_text(json.dumps({"old": old, "new": new}))
        process = subprocess.Popen(
            [sys.executable, __file__, "--worker", phase, "--output", str(directory)]
        )
        await_file(directory / "ready", process)
        if phase == "during_commit":
            (directory / "continue").write_text("continue")
            await_file(directory / "commit_enter", process)
        acknowledged_before_kill = (directory / "commit_ack").exists()
        # The ONLY process termination operation; reached solely by explicit CLI opt-in.
        process.kill()
        process.wait(timeout=30)
        if phase == "during_commit" and acknowledged_before_kill:
            raise RuntimeError("during-COMMIT arm timing inconclusive; no retry loop performed")
        # First fresh process performs only recovery/open/close, preserving the
        # significant recovery-only shutdown path for a second fresh verifier.
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys,duckdb; c=duckdb.connect(sys.argv[1]); "
                "c.execute('SELECT 1'); c.close()",
                str(db.path),
            ],
            check=True,
        )
        subprocess.run(
            [sys.executable, __file__, "--verify", phase, "--output", str(directory)], check=True
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--execute-authorized", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--verify", choices=PHASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.output, args.worker)
    elif args.verify:
        verify(args.output, args.verify)
    elif args.execute_authorized:
        if args.output is None:
            parser.error("--output must name a NEW disposable directory")
        run_authorized(args.output)
    else:
        print(
            json.dumps(
                {
                    "termination_authorized": False,
                    "executed": False,
                    "phases": PHASES,
                    "recovery": [
                        "fresh-process minimal reopen/close",
                        "fresh-process indexed/full-scan verifier",
                    ],
                    "checks": [
                        "complete old/new",
                        "post-ack new",
                        "identical retry",
                        "changed retry",
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
