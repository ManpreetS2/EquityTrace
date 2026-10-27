"""Finite offline 25k-envelope qualification; cooperative boundaries, never termination."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb
from qualify_sec_memory import process_memory
from qualify_sec_recovery import domain_state
from sec_ingestion_synthetic import make_payloads
from sec_recovery_checks import publish

from equitytrace.database import Database
from equitytrace.models import ResolvedTicker
from equitytrace.repositories import ingestion
from equitytrace.repositories.facts import FactsRepository

PROFILES = ("baseline", "large_text", "diverse")
CEILING = 1024**3


class MemoryCeiling(RuntimeError):
    """Stop cooperatively at the next measured boundary."""


def fixture(count, profile, scale=1.0):
    submissions, facts = make_payloads(count, scale)
    concepts = facts["facts"]["us-gaap"]
    if profile == "large_text":
        for item in concepts.values():
            item["description"] = ("Detailed financial accounting policy and disclosure. " * 100)[
                :4096
            ]
            item["label"] = ("Financial accounting evidence " * 20)[:512]
    elif profile == "diverse":
        observations = [obs for item in concepts.values() for obs in item["units"]["USD"]]
        filing_count = min(500, (count + 39) // 40)
        recent = submissions["filings"]["recent"]
        accessions = [f"0000999999-26-{i + 1:06d}" for i in range(filing_count)]
        for key, values in list(recent.items()):
            recent[key] = [values[i % len(values)] for i in range(filing_count)]
        recent["accessionNumber"] = accessions
        recent["primaryDocument"] = [
            f"annual-{i}-" + "metadata-" * 50 + ".htm" for i in range(filing_count)
        ]
        recent["fileNumber"] = [f"001-{i:06d}" for i in range(filing_count)]
        concepts = {}
        for i, obs in enumerate(observations):
            obs["accn"] = accessions[i // 40]
            item = concepts.setdefault(
                f"DiverseConcept{i % min(2048, count):04d}",
                {
                    "label": f"Diverse financial evidence {i % min(2048, count)}",
                    "description": ("Financial disclosure with diverse dimensions. " * 30)[:1024],
                    "units": {},
                },
            )
            item["units"].setdefault("USD" if i % 2 else "shares", []).append(obs)
        facts["facts"]["us-gaap"] = concepts
    elif profile != "baseline":
        raise ValueError("Unknown profile")
    return submissions, facts


def qualify(profile, output):
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    tracemalloc.start()
    count = 20000 if profile == "diverse" else 25000
    db = Database(output / "synthetic.duckdb")
    phase = "startup"
    breached = False
    batch = 0
    rollback = False
    payload = None
    totals = []
    expanded = {}
    db.initialize()

    def sample(boundary, conn=None):
        nonlocal breached
        rss, peak = process_memory()
        py, py_peak = tracemalloc.get_traced_memory()
        categories = (
            dict(conn.execute("SELECT tag, memory_usage_bytes FROM duckdb_memory()").fetchall())
            if conn
            else {}
        )
        wal = Path(str(db.path) + ".wal")
        row = {
            "phase": phase,
            "boundary": boundary,
            "elapsed_seconds": time.perf_counter() - started,
            "rss_bytes": rss,
            "peak_rss_bytes": peak,
            "python_bytes": py,
            "python_peak_bytes": py_peak,
            "duckdb": categories,
            "database_bytes": db.path.stat().st_size,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        }
        with (output / "boundaries.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if peak > CEILING:
            breached = True
            raise MemoryCeiling(f"Peak RSS {peak} exceeds {CEILING} at {phase}/{boundary}")

    original_init = FactsRepository.__init__
    original_validate = ingestion.validate_snapshot
    original_commit = ingestion.IngestionRepository._commit

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, params=None):
            nonlocal batch
            result = self.conn.execute(sql, params)
            if sql.startswith("INSERT INTO financial_facts"):
                batch += 1
                sample(f"fact_batch_{batch}", self.conn)
                if rollback and batch == 2:
                    raise RuntimeError("planned later-batch rollback")
            return result

    def init(self, conn):
        original_init(self, Connection(conn))

    def typed(*args, **kwargs):
        facts = args[3]
        expanded[phase] = {
            "facts": len(facts),
            "filings": len(args[2]),
            "fact_string_bytes": sum(
                len(v.encode("utf-8"))
                for f in facts
                for v in f.model_dump().values()
                if isinstance(v, str)
            ),
        }
        sample("typed_inputs")
        return original_validate(*args, **kwargs)

    def commit(conn):
        sample("before_commit", conn)
        original_commit(conn)
        sample("after_commit", conn)

    status = "passed"
    error = None
    try:
        sample("imports_and_open")
        if profile == "diverse":
            phase = "unrelated_history"
            for i in range(5):
                publish(db, 1000, 1, cik=f"{800000 + i:010d}")
                sample(f"unrelated_{i + 1}")
        with db.session() as conn:
            conn.execute("""INSERT INTO ingestion_runs
                (run_id,ticker,cik,started_at,finished_at,status)
                SELECT 'historical-' || range::VARCHAR,'SYNTH','0000999999',CURRENT_TIMESTAMP,
                       CURRENT_TIMESTAMP,'failed' FROM range(5000)""")
        sample("run_history_5000")
        with (
            patch.object(
                ingestion,
                "resolve_ticker",
                return_value=ResolvedTicker(
                    ticker="SYNTH", cik="0000999999", company_name="Synthetic"
                ),
            ),
            patch.object(ingestion, "fetch_all_submissions", side_effect=lambda *_: payload[0]),
            patch.object(ingestion, "fetch_company_facts", side_effect=lambda *_: payload[1]),
            patch.object(ingestion, "validate_snapshot", typed),
            patch.object(FactsRepository, "__init__", init),
            patch.object(ingestion.IngestionRepository, "_commit", staticmethod(commit)),
        ):
            repo = ingestion.IngestionRepository(db)
            expected = None
            for phase, scale in (
                ("initial", 1.0),
                ("identical", 1.0),
                ("changed", 2.0),
                ("rollback", 3.0),
                ("retry", 2.0),
            ):
                began = time.perf_counter()
                batch = 0
                rollback = phase == "rollback"
                payload = fixture(count, profile, scale)
                sample("synthetic_payload")
                encoded = [json.dumps(p, separators=(",", ":")).encode() for p in payload]
                sample("json_bytes")
                payload = tuple(json.loads(p) for p in encoded)
                sample("json_parsed_with_response_bytes")
                del encoded
                try:
                    repo.ingest_ticker(None, "SYNTH")
                    assert not rollback
                except ingestion.IngestionError as exc:
                    if breached:
                        raise MemoryCeiling("Cooperative insertion ceiling") from None
                    if not rollback or "planned later-batch rollback" not in str(exc):
                        raise
                sample("closed_or_rolled_back")
                with db.session() as conn:
                    actual = domain_state(conn)
                    run = conn.execute(
                        "SELECT run_id,status,fact_count FROM ingestion_runs "
                        "WHERE ticker='SYNTH' AND status='success' "
                        "ORDER BY finished_at DESC LIMIT 1"
                    ).fetchone()
                    assert (
                        conn.execute("SELECT count(*) FROM financial_facts").fetchone()[0] == 25000
                    )
                if phase in ("identical", "rollback", "retry"):
                    assert actual == expected
                if phase == "changed":
                    assert actual != expected
                if not rollback:
                    assert run[1:] == ("success", count)
                    assert repo.reconcile_run(run[0]) == "success"
                    sample("reconciliation_complete")
                expected = actual
                totals.append(
                    {"phase": phase, "seconds": time.perf_counter() - began, "domain": actual}
                )
    except MemoryCeiling as exc:
        status = "memory_ceiling_exceeded"
        error = str(exc)
    except Exception as exc:
        status = "correctness_or_harness_failure"
        error = repr(exc)
        raise
    finally:
        report = {
            "profile": profile,
            "status": status,
            "error": error,
            "target_facts": count,
            "total_facts": 25000,
            "ceiling_bytes": CEILING,
            "peak_rss_bytes": process_memory()[1],
            "seconds": time.perf_counter() - started,
            "platform": platform.platform(),
            "python": sys.version,
            "duckdb": duckdb.__version__,
            "host_limit_enforced": False,
            "monitoring": "tracemalloc, process high-water RSS, fsync each boundary",
            "expanded_inputs": expanded,
            "completed": totals,
            "process_termination_executed": False,
        }
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return status == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not qualify(args.profile, args.output):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
