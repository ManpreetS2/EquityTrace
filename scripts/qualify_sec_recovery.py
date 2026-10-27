"""Finite disposable recovery qualification; termination requires separate authorization.

Plan and self-check never terminate a process. The pending-COMMIT arm is
explicitly ambiguous; no native-COMMIT timing claim or automatic retry loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb
from sec_recovery_checks import (
    OTHER,
    TARGET,
    RecoveryMismatch,
    digest_query,
    probe_specs,
    publish,
    state,
    verify_first_reads,
    write_json,
)

from equitytrace.database import Database
from equitytrace.repositories.ingestion import IngestionRepository

PHASES = ("before_commit", "pre_or_during_commit", "after_ack")
OPEN_SEQUENCES = ("direct_index_first", "recovery_close_index_first")
LEASE_SECONDS = 90
SCRIPT = Path(__file__).resolve()


def plan():
    return {
        "termination_authorized": False,
        "executed": False,
        "cases": 6,
        "phases": PHASES,
        "open_sequences": OPEN_SEQUENCES,
        "facts_per_target": 2501,
        "facts_per_unrelated_issuer": 17,
        "first_data_query": "profiled SELECT * FROM financial_facts WHERE cik = ?",
        "planner_note": "index_scan_max_count=1000000 for diagnostic indexed reads only",
        "phase_limit": "Call-adjacent ambiguity includes post-return before durable ACK marker",
        "ack_race": "new must survive, but ambiguous-arm coverage remains INCONCLUSIVE",
        "checks": [
            "all five tables/all columns",
            "indexed/unfiltered-scan agreement",
            "run ID/status/counts",
            "PIT",
            "cross-issuer",
            "reconciliation",
            "retries",
        ],
        "product_review": "guarded reconciliation implemented; crash qualification remains pending",
    }


def event(directory, name, run_id):
    case = json.loads((directory / "case.json").read_text())
    previous = [
        json.loads(path.read_text())["ordinal"]
        for path in (
            directory / f"{label}.json" for label in ("ready", "commit_call_pending", "commit_ack")
        )
        if path.exists()
    ]
    write_json(
        directory / f"{name}.json",
        {
            "name": name,
            "run_id": run_id,
            "case_id": case["case_id"],
            "pid": os.getpid(),
            "monotonic_ns": time.monotonic_ns(),
            "ordinal": max(previous, default=-1) + 1,
        },
    )


def await_file(path, process=None, timeout=LEASE_SECONDS):
    until = time.monotonic() + timeout
    while not path.exists():
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Worker exited before {path.name}: {process.returncode}")
        if time.monotonic() >= until:
            raise TimeoutError(f"No marker {path}; no timeout kill is issued")
        time.sleep(0.01)


def hold(directory):
    until = time.monotonic() + LEASE_SECONDS
    while not (directory / "continue").exists():
        if (directory / "abort").exists() or time.monotonic() >= until:
            raise RuntimeError("Cooperative boundary abort/lease expired")
        time.sleep(0.01)


def prepare_case(directory, phase, sequence, count=2501):
    if phase not in PHASES or sequence not in OPEN_SEQUENCES:
        raise ValueError("Unknown qualification phase/open sequence")
    directory.mkdir(parents=True, exist_ok=False)
    write_json(
        directory / "case.json",
        {
            "case_id": uuid.uuid4().hex,
            "phase": phase,
            "sequence": sequence,
            "count": count,
            "database": "synthetic.duckdb",
            "fixture": "offline SEC synthetic",
        },
    )
    db = Database(directory / "synthetic.duckdb")
    db.initialize()
    publish(db, 17, 1, cik=OTHER)
    publish(db, count, 1)


def worker(directory):
    case = json.loads((directory / "case.json").read_text())
    db = Database(directory / case["database"])
    run_id = None

    def started(value):
        nonlocal run_id
        run_id = value

    def boundary(conn):
        with db.session() as old_conn:
            ids = [
                old_conn.execute(
                    "SELECT fact_id FROM financial_facts WHERE cik=? ORDER BY fact_id LIMIT 1",
                    [TARGET],
                ).fetchone()[0],
                conn.execute(
                    "SELECT fact_id FROM financial_facts WHERE cik=? ORDER BY fact_id LIMIT 1",
                    [TARGET],
                ).fetchone()[0],
            ]
            specs = probe_specs(run_id, ids)
            old, new = state(old_conn, specs), state(conn, specs)
        for name in specs:
            if name.startswith("other_") and old["probes"][name] != new["probes"][name]:
                raise RecoveryMismatch("Candidate mutated unrelated issuer")
        status = conn.execute(
            "SELECT status, issuer_count, security_count, filing_count, fact_count "
            "FROM ingestion_runs WHERE run_id=?",
            [run_id],
        ).fetchone()
        expected = ("success", 1, 3, (case["count"] + 199) // 200, case["count"])
        if status != expected:
            raise RecoveryMismatch(f"Candidate run/count mismatch: {status}")
        write_json(
            directory / "expected.json",
            {
                "case_id": case["case_id"],
                "run_id": run_id,
                "fact_ids": ids,
                "old": old,
                "new": new,
            },
        )
        if case["phase"] in ("before_commit", "pre_or_during_commit"):
            event(directory, "ready", run_id)
            hold(directory)
        event(directory, "commit_call_pending", run_id)
        conn.execute("COMMIT")
        event(directory, "commit_ack", run_id)  # Strictly after returned COMMIT.
        if case["phase"] == "after_ack":
            event(directory, "ready", run_id)
            hold(directory)

    publish(db, case["count"], 2, boundary=boundary, started=started)


def classify_phase(directory):
    case = json.loads((directory / "case.json").read_text())
    manifest = json.loads((directory / "expected.json").read_text())
    if manifest.get("case_id") != case["case_id"]:
        raise RecoveryMismatch("Oracle belongs to another case")
    events = {}
    for name in ("ready", "commit_call_pending", "commit_ack"):
        path = directory / f"{name}.json"
        if path.exists():
            item = json.loads(path.read_text())
            if (
                item["case_id"] != case["case_id"]
                or item["run_id"] != manifest["run_id"]
                or item.get("name") != name
                or not isinstance(item.get("ordinal"), int)
                or item["ordinal"] < 0
            ):
                raise RecoveryMismatch("Stale/unrelated synchronization marker")
            events[name] = item
    if "ready" not in events or len({x["pid"] for x in events.values()}) != 1:
        raise RecoveryMismatch("No single synchronized ready worker")
    if len({x["ordinal"] for x in events.values()}) != len(events):
        raise RecoveryMismatch("Synchronization ordinals are not distinct")
    if "commit_ack" in events and (
        "commit_call_pending" not in events
        or events["commit_ack"]["ordinal"] <= events["commit_call_pending"]["ordinal"]
    ):
        raise RecoveryMismatch("Acknowledgement ordering invalid")
    if case["phase"] == "before_commit":
        if "commit_call_pending" in events:
            raise RecoveryMismatch("Pre-COMMIT case advanced beyond held boundary")
        return "proven_before_commit", ["old"], True
    if case["phase"] == "after_ack":
        if "commit_ack" not in events:
            raise RecoveryMismatch("Post-ack case lacks returned-COMMIT evidence")
        if events["ready"]["ordinal"] <= events["commit_ack"]["ordinal"]:
            raise RecoveryMismatch("Post-ack ready preceded acknowledgement")
        return "proven_after_ack", ["new"], True
    if "commit_call_pending" not in events:
        raise RecoveryMismatch("Ambiguous arm never reached pending COMMIT call")
    if "commit_ack" in events:
        return "acknowledged_commit_race", ["new"], False
    return "ambiguous_commit_boundary_unacknowledged", ["old", "new"], True


def domain_state(conn):
    result = {}
    for table, key in (
        ("issuers", "cik"),
        ("securities", "ticker,cik"),
        ("filings", "accession_number"),
        ("financial_facts", "fact_id"),
    ):
        columns = [
            r[1]
            for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            if r[1] not in ("created_at", "updated_at")
        ]
        result[table] = digest_query(
            conn, f"SELECT {','.join(columns)} FROM {table} ORDER BY {key}"
        )
    return result


def retry_case(directory):
    verdict = json.loads((directory / "verified.json").read_text())
    if verdict["status"] != "passed":
        raise RecoveryMismatch("Retries require complete verified recovery")
    case = json.loads((directory / "case.json").read_text())
    original = json.loads((directory / "expected.json").read_text())
    db = Database(directory / "synthetic.duckdb")
    with db.session() as conn:
        before = domain_state(conn)
    expected_status = "success" if verdict["recovered"] == "new" else "failed"
    if IngestionRepository(db).reconcile_run(original["run_id"]) != expected_status:
        raise RecoveryMismatch("Reconciliation changed committed-run meaning")
    version = 2 if verdict["recovered"] == "new" else 1
    for label, next_version in (("identical", version), ("changed", version + 1)):
        ids = []
        publish(db, case["count"], next_version, started=ids.append)
        with db.session() as conn:
            after = domain_state(conn)
            if (after == before) != (label == "identical"):
                raise RecoveryMismatch("Retry content does not match intended replacement")
            specs = probe_specs(ids[0], original["fact_ids"])
            current = state(conn, specs)
            for name, value in original[verdict["recovered"]]["probes"].items():
                if name.startswith("other_") and current["probes"][name] != value:
                    raise RecoveryMismatch("Retry mutated unrelated issuer")
            run = conn.execute(
                "SELECT status, fact_count FROM ingestion_runs WHERE run_id=?", ids
            ).fetchone()
            if run != ("success", case["count"]):
                raise RecoveryMismatch("Retry success/count mismatch")
            candidate = conn.execute(
                "SELECT status FROM ingestion_runs WHERE run_id=?", [original["run_id"]]
            ).fetchone()[0]
            if candidate != expected_status:
                raise RecoveryMismatch("Committed candidate run was demoted")
        expected = {"run_id": ids[0], "fact_ids": original["fact_ids"], "old": {}, "new": current}
        if verify_first_reads(directory, expected, label=label) != "new":
            raise RecoveryMismatch("Retry indexed/full state mismatch")
        before = after
    write_json(directory / "retries.json", {"status": "passed", "rounds": ["identical", "changed"]})


def verify_case(directory, normal_exit=False):
    manifest = json.loads((directory / "expected.json").read_text())
    if normal_exit:
        classification, allowed, covered = "normal_exit_control", ["new"], False
    else:
        receipt = json.loads((directory / "termination.json").read_text())
        ready = json.loads((directory / "ready.json").read_text())
        if (
            receipt["method"] != "process.kill"
            or receipt["exit_code"] == 0
            or receipt["pid"] != ready["pid"]
            or receipt["case_id"] != manifest["case_id"]
            or receipt["run_id"] != manifest["run_id"]
        ):
            raise RecoveryMismatch("Missing or inconsistent actual termination receipt")
        classification, allowed, covered = classify_phase(directory)
    recovered = verify_first_reads(directory, manifest)
    if recovered not in allowed:
        raise RecoveryMismatch(f"{classification} recovered disallowed {recovered} state")
    write_json(
        directory / "verified.json",
        {
            "status": "passed",
            "recovered": recovered,
            "classification": classification,
            "phase_coverage": covered,
            "run_id": manifest["run_id"],
            "termination_executed": not normal_exit,
        },
    )


def child(directory, mode):
    with (directory / f"{mode}.log").open("xb") as log:
        return subprocess.Popen(
            [sys.executable, str(SCRIPT), f"--{mode}", "--output", str(directory)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )


def checked_wait(process, timeout=120):
    # Timeout raises and stops the matrix; Popen.wait never kills a child.
    code = process.wait(timeout=timeout)
    if code:
        raise RuntimeError(f"Qualification child {process.pid} exited {code}")


def source_hashes():
    root = SCRIPT.parents[1]
    paths = (
        "scripts/qualify_sec_recovery.py",
        "scripts/sec_recovery_checks.py",
        "scripts/sec_ingestion_synthetic.py",
        "pyproject.toml",
        "uv.lock",
        *(p.relative_to(root).as_posix() for p in sorted((root / "src/equitytrace").rglob("*.py"))),
    )
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}


def run(root, *, execute_authorized=False, self_check=False):
    existed = root.exists()
    try:
        _run_matrix(root, execute_authorized=execute_authorized, self_check=self_check)
    except BaseException as exc:
        # Never alter a previously existing directory when refusing its reuse.
        if not existed and root.exists():
            try:
                write_json(root / "failure.json", {"type": type(exc).__name__, "error": str(exc)})
            except Exception as evidence_error:
                exc.add_note(f"Failure evidence could not be written: {evidence_error}")
        raise


def _run_matrix(root, *, execute_authorized=False, self_check=False):
    if execute_authorized == self_check:
        raise ValueError("Choose exactly one authorized run or normal self-check")
    root.mkdir(parents=True, exist_ok=False)
    write_json(
        root / "environment.json",
        {
            "python": sys.version,
            "platform": platform.platform(),
            "duckdb": duckdb.__version__,
            "harness_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
            "source_sha256": source_hashes(),
            "termination_authorized": execute_authorized,
        },
    )
    coverage = []
    for phase in PHASES:
        for sequence in OPEN_SEQUENCES:
            directory = root / f"{phase}-{sequence}"
            prepare_case(directory, phase, sequence)
            process = child(directory, "worker")
            try:
                await_file(directory / "ready.json", process)
                if self_check:
                    (directory / "continue").write_text("ordinary completion")
                    checked_wait(process)
                else:
                    if phase == "pre_or_during_commit":
                        (directory / "continue").write_text("advance toward COMMIT")
                        await_file(directory / "commit_call_pending.json", process)
                    # Only termination operation, unreachable in plan/self-check.
                    process.kill()
                    process.wait(timeout=30)
                    write_json(
                        directory / "termination.json",
                        {
                            "pid": process.pid,
                            "exit_code": process.returncode,
                            "classification": classify_phase(directory)[0],
                            "method": "process.kill",
                            "case_id": json.loads((directory / "case.json").read_text())["case_id"],
                            "run_id": json.loads((directory / "expected.json").read_text())[
                                "run_id"
                            ],
                        },
                    )
            except BaseException:
                (directory / "abort").write_text("cooperative abort; stop matrix")
                raise
            if sequence == "recovery_close_index_first":
                checked_wait(child(directory, "minimal-open"))
            checked_wait(child(directory, "verify-normal" if self_check else "verify"))
            checked_wait(child(directory, "retry"))
            coverage.append(json.loads((directory / "verified.json").read_text()))
    complete = not self_check and all(item["phase_coverage"] for item in coverage)
    write_json(
        root / "matrix.json",
        {
            "status": "passed"
            if complete
            else "normal_controls_only"
            if self_check
            else "INCONCLUSIVE",
            "cases": coverage,
            "termination_executed": execute_authorized,
        },
    )
    if execute_authorized and not complete:
        raise RecoveryMismatch("Acknowledgement race leaves ambiguous-arm coverage incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    for mode in (
        "plan",
        "execute-authorized",
        "self-check",
        "worker",
        "verify",
        "verify-normal",
        "minimal-open",
        "retry",
    ):
        modes.add_argument(f"--{mode}", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.plan or not any(v for k, v in vars(args).items() if k != "output"):
        print(json.dumps(plan(), indent=2))
        return
    if args.output is None:
        parser.error("--output must name a disposable qualification directory")
    if args.worker:
        worker(args.output)
    elif args.minimal_open:
        with duckdb.connect(str(args.output / "synthetic.duckdb")) as conn:
            conn.execute("SELECT 1")
    elif args.verify or args.verify_normal:
        verify_case(args.output, normal_exit=args.verify_normal)
    elif args.retry:
        retry_case(args.output)
    else:
        run(args.output, execute_authorized=args.execute_authorized, self_check=args.self_check)


if __name__ == "__main__":
    main()
