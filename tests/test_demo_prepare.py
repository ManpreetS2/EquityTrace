"""Safe, idempotent preparation of the dedicated synthetic demo database."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import duckdb
import pytest

from equitytrace.demo import prepare as prepare_module
from equitytrace.demo.data import build_dataset
from equitytrace.demo.prepare import (
    DemoPaths,
    DemoPreparationError,
    database_digest,
    prepare_demo,
    read_manifest,
)
from equitytrace.demo.service import DemoService


def _count(path: Path, table: str) -> int:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else -1
    finally:
        conn.close()


def test_prepare_is_deterministic_across_fresh_databases(tmp_path: Path) -> None:
    first = prepare_demo(tmp_path / "a")
    second = prepare_demo(tmp_path / "b")
    expected = build_dataset().digest()
    assert first.action == second.action == "created"
    assert first.digest == second.digest == expected
    assert database_digest(first.paths.database) == database_digest(second.paths.database)
    manifest = read_manifest(first.paths)
    assert manifest is not None and manifest["synthetic"] is True
    assert manifest["content_digest"] == expected


def test_repeated_prepare_changes_nothing_and_keeps_saved_runs(demo_dir: Path) -> None:
    service = DemoService(demo_dir)
    validation = service.validate_strategy(service.presets()[0].payload)
    assert validation.strategy is not None
    service.run_strategy(validation.strategy, token="keep-me")
    paths = DemoPaths(demo_dir)
    before = {
        table: _count(paths.database, table)
        for table in ("financial_facts", "daily_price_bars", "ingestion_runs", "portfolio_runs")
    }
    for _ in range(3):
        assert service.prepare().action == "unchanged"
    after = {table: _count(paths.database, table) for table in before}
    assert after == before
    assert before["financial_facts"] == 168 and before["portfolio_runs"] == 1
    assert before["ingestion_runs"] == 0  # no fake SEC ingestion records


def test_foreign_database_is_never_overwritten_even_with_overwrite(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    directory.mkdir()
    foreign = DemoPaths(directory).database
    conn = duckdb.connect(str(foreign))
    conn.execute("CREATE TABLE precious AS SELECT 42 AS answer")
    conn.close()
    before = foreign.read_bytes()
    for overwrite in (False, True):
        with pytest.raises(DemoPreparationError, match="not marked as an EquityTrace"):
            prepare_demo(directory, overwrite=overwrite)
    assert foreign.read_bytes() == before
    assert not DemoPaths(directory).manifest.exists()


def test_user_database_path_setting_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_db = tmp_path / "user.duckdb"
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(user_db))
    prepare_demo(tmp_path / "demo")
    assert not user_db.exists()


def test_tampered_demo_is_detected_and_only_rebuilt_on_explicit_overwrite(demo_dir: Path) -> None:
    paths = DemoPaths(demo_dir)
    conn = duckdb.connect(str(paths.database))
    conn.execute("UPDATE financial_facts SET value = value + 1 WHERE concept = 'Assets'")
    conn.close()
    with pytest.raises(DemoPreparationError, match="discard saved demo runs"):
        prepare_demo(demo_dir)
    # A tampered database is also not reported as trusted/prepared by a cheap status check
    # only when counts differ; the full digest check above is what blocks reuse.
    outcome = prepare_demo(demo_dir, overwrite=True)
    assert outcome.action == "replaced"
    assert database_digest(paths.database) == build_dataset().digest()


def test_explicit_overwrite_resets_saved_runs_on_a_valid_demo(demo_dir: Path) -> None:
    service = DemoService(demo_dir)
    validation = service.validate_strategy(service.presets()[0].payload)
    assert validation.strategy is not None
    service.run_strategy(validation.strategy, token="reset-me")
    assert service.status().saved_run_count == 1
    assert service.prepare().action == "unchanged"
    assert service.prepare(overwrite=True).action == "replaced"
    assert service.status().saved_run_count == 0
    assert service.status().prepared is True


def test_failed_build_leaves_no_trusted_partial_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "demo"

    def boom(path: Path, dataset: object) -> None:
        path.write_bytes(b"partial")
        raise RuntimeError("disk full")

    monkeypatch.setattr(prepare_module, "_populate", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        prepare_demo(directory)
    assert sorted(p.name for p in directory.iterdir()) == []
    assert DemoService(directory).status().prepared is False


def test_failed_rebuild_keeps_the_existing_valid_demo(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = DemoPaths(demo_dir)
    before = paths.database.read_bytes()

    def boom(path: Path, dataset: object) -> None:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(prepare_module, "_populate", boom)
    with pytest.raises(RuntimeError, match="interrupted"):
        prepare_demo(demo_dir, overwrite=True)
    assert paths.database.read_bytes() == before
    assert DemoService(demo_dir).status().prepared is True
    assert [p.name for p in demo_dir.iterdir() if p.name.startswith(".building")] == []


def test_preparation_and_reads_make_no_network_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    service = DemoService(tmp_path / "demo")
    service.prepare()
    assert service.status().prepared
    assert service.securities()
    assert os.path.exists(service.paths.database)


def test_preparation_does_not_balloon_process_memory(tmp_path: Path) -> None:
    """Regression: one transaction over every price bar peaked >1 GiB above the baseline."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    code = "\n".join(
        [
            "import sys",
            f"sys.path[:0] = [{str(root / 'scripts')!r}]",
            "from qualify_sec_memory import process_memory",
            "from equitytrace.demo.prepare import prepare_demo",
            "from pathlib import Path",
            "before = process_memory()[1]",
            f"prepare_demo(Path({str(tmp_path / 'mem-demo')!r}))",
            "print((process_memory()[1] - before) / 2**20)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    growth_mib = float(result.stdout.strip().splitlines()[-1])
    assert growth_mib < 600, f"preparation raised peak memory by {growth_mib:.0f} MiB"
