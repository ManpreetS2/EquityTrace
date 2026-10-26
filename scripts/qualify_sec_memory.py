"""Normal-exit memory/rollback qualification. Never terminates a process.

Run each size in a fresh interpreter:
  python scripts/qualify_sec_memory.py --facts 25000 --output <new-directory>
  python scripts/qualify_sec_memory.py --facts 100000 --output <new-directory>
No providers, dependencies, engine flags or production databases are used.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import platform
import sys
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb
from sec_ingestion_synthetic import fact_digest, make_payloads

from equitytrace.database import Database
from equitytrace.models import ResolvedTicker
from equitytrace.repositories import ingestion
from equitytrace.repositories.facts import FactsRepository


def process_memory() -> tuple[int, int]:
    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        values = Counters()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(values), ctypes.sizeof(values)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return values.WorkingSetSize, values.PeakWorkingSetSize
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = peak if sys.platform == "darwin" else peak * 1024
    current = peak
    if sys.platform.startswith("linux"):
        import os

        current = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return current, peak


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=int, choices=[25_000, 100_000], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    db = Database(args.output / "synthetic.duckdb")
    db.initialize()
    samples = []
    phase = "startup"
    started = time.perf_counter()
    tracemalloc.start()
    batch_count = 0
    force_rollback = False

    def sample(conn=None):
        rss, peak = process_memory()
        current_python, peak_python = tracemalloc.get_traced_memory()
        memory = (
            conn.execute("SELECT sum(memory_usage_bytes) FROM duckdb_memory()").fetchone()[0]
            if conn
            else 0
        )
        samples.append(
            {
                "phase": phase,
                "elapsed_seconds": time.perf_counter() - started,
                "rss_bytes": rss,
                "process_peak_rss_bytes": peak,
                "python_traced_bytes": current_python,
                "python_traced_peak_bytes": peak_python,
                "duckdb_tracked_bytes": memory,
                "database_bytes": db.path.stat().st_size,
                "wal_bytes": Path(str(db.path) + ".wal").stat().st_size
                if Path(str(db.path) + ".wal").exists()
                else 0,
            }
        )

    original_init = FactsRepository.__init__
    validate = ingestion.validate_snapshot
    commit = ingestion.IngestionRepository._commit
    payloads = None

    class InstrumentedConnection:
        def __init__(self, conn):
            self.conn = conn

        def executemany(self, sql, rows):
            nonlocal batch_count
            result = self.conn.executemany(sql, rows)
            batch_count += 1
            sample(self.conn)
            if force_rollback and batch_count == max(1, args.facts // 4000):
                raise RuntimeError("qualification rollback between batches")
            return result

        def __getattr__(self, name):
            return getattr(self.conn, name)

    def init(self, conn):
        original_init(self, InstrumentedConnection(conn))

    def typed_inputs(*a, **kw):
        sample()
        return validate(*a, **kw)

    def committed(conn):
        commit(conn)
        sample(conn)

    durations = {}
    with (
        patch.object(
            ingestion,
            "resolve_ticker",
            return_value=ResolvedTicker(ticker="SYNTH", cik="0000999999", company_name="Synthetic"),
        ),
        patch.object(ingestion, "fetch_all_submissions", side_effect=lambda *_: payloads[0]),
        patch.object(ingestion, "fetch_company_facts", side_effect=lambda *_: payloads[1]),
        patch.object(ingestion, "validate_snapshot", typed_inputs),
        patch.object(FactsRepository, "__init__", init),
        patch.object(ingestion.IngestionRepository, "_commit", staticmethod(committed)),
    ):
        repo = ingestion.IngestionRepository(db)
        expected = None
        for phase, scale in [
            ("initial", 1.0),
            ("identical", 1.0),
            ("changed", 2.0),
            ("rollback", 3.0),
            ("retry", 2.0),
        ]:
            began = time.perf_counter()
            batch_count = 0
            force_rollback = phase == "rollback"
            tracemalloc.reset_peak()
            payloads = make_payloads(args.facts, scale)
            sample()
            try:
                repo.ingest_ticker(None, "SYNTH")
                assert not force_rollback
            except ingestion.IngestionError:
                if not force_rollback:
                    raise
            sample()
            with db.session() as conn:
                observed = fact_digest(conn)
            assert observed[0] == args.facts
            if phase == "identical":
                assert observed == expected
            if phase in ("rollback", "retry"):
                assert observed == expected
            expected = observed
            durations[phase] = time.perf_counter() - began
            print(
                json.dumps(
                    {"phase": phase, "seconds": durations[phase], "peak_rss": process_memory()[1]}
                ),
                flush=True,
            )
    peak = max(s["process_peak_rss_bytes"] for s in samples)
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "duckdb": duckdb.__version__,
        "facts": args.facts,
        "normal_exits_only": True,
        "process_termination_executed": False,
        "host_note": "Host RAM must be recorded separately; no 4-GiB host claim from process RSS.",
        "peak_rss_bytes": peak,
        "target_rss_bytes": 1024**3,
        "memory_gate_passed": peak <= 1024**3,
        "durations_seconds": durations,
        "samples": samples,
    }
    (args.output / "measurements.json").write_text(json.dumps(report, indent=2) + "\n")
    if peak > 1024**3:
        raise SystemExit("MEMORY GATE FAILED: peak RSS exceeds 1 GiB")


if __name__ == "__main__":
    main()
