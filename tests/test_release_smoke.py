"""Release-level smoke: one offline pass through install/import → evidence → research → exports.

Deliberately a single narrative so it catches interactions that focused tests miss. It uses the
shipped synthetic dataset and the public service boundary only, with the network blocked.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from equitytrace.demo.prepare import DemoPaths, database_digest
from equitytrace.demo.service import DemoService, decision_time
from equitytrace.portfolio.models import PortfolioRunStatus
from equitytrace.reporting.export import export_run_json, write_html_report
from equitytrace.strategy.models import fingerprint_request

ROOT = Path(__file__).resolve().parents[1]


def test_core_package_and_cli_import_without_streamlit() -> None:
    """The optional UI dependency must never be required by the core package or the CLI."""
    code = (
        "import sys; sys.modules['streamlit'] = None\n"
        "import equitytrace, equitytrace.cli, equitytrace.demo.service, equitytrace.demo.prepare\n"
        "from equitytrace.cli import app\n"
        "print(equitytrace.__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.0.0"


def test_offline_end_to_end_release_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)

    # 1. Prepare: deterministic, verified, idempotent.
    service = DemoService(tmp_path / "demo")
    assert service.status().prepared is False
    created = service.prepare()
    assert created.action == "created" and service.prepare().action == "unchanged"
    database = DemoPaths(tmp_path / "demo").database
    assert database_digest(database) == created.digest

    # 2. Statements and factors: canonical values with provenance at an as-of instant.
    view = service.snapshot_view("SYAL", 2023, decision_time(date(2024, 3, 29)))
    revenue = view.snapshot.get_value("revenue")
    assert revenue is not None and revenue.value == 1280 * 1_000_000.0
    assert revenue.provenance.accession_number and revenue.provenance.available_at
    assert {f.factor for f in view.factors if f.valid} >= {"roa", "revenue_growth"}

    # 3. As-of availability: the same lookup before the filing is public has no value.
    early = service.snapshot_view("SYAL", 2023, decision_time(date(2024, 2, 7)))
    early_revenue = early.snapshot.get_value("revenue")
    assert early_revenue is not None and early_revenue.value is None

    # 4. Validate, then run the real engine once; persistence and reload agree.
    outcome = service.validate_strategy(service.presets()[0].payload)
    assert outcome.ok and outcome.strategy is not None and outcome.fingerprint
    result = service.run_strategy(outcome.strategy, token="release-smoke")
    assert result.status is PortfolioRunStatus.SUCCESS
    reloaded = DemoService(tmp_path / "demo").load_run(result.run_id)  # a "restarted" service
    assert reloaded == result
    assert fingerprint_request(reloaded.request) == outcome.fingerprint

    # 5. Reports and exports regenerate byte-identically from the persisted run.
    html_one, html_two = DemoService.report_html(reloaded), DemoService.report_html(reloaded)
    assert html_one == html_two and "Research status: SUCCESS" in html_one
    write_html_report(tmp_path / "report.html", html_one)
    request_path, result_path = export_run_json(reloaded, tmp_path / "export")
    digests = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (request_path, result_path)
    }
    export_run_json(reloaded, tmp_path / "export-again")
    for name, digest in digests.items():
        assert hashlib.sha256((tmp_path / "export-again" / name).read_bytes()).hexdigest() == digest
    exported = json.loads(result_path.read_text(encoding="utf-8"))
    assert exported["export_metadata"]["run_id"] == result.run_id
    assert exported["result"]["status"] == "success"

    # 6. Unavailable diagnostics fail closed with an explanation and no performance.
    for preset in service.presets():
        if preset.expectation != "unavailable":
            continue
        diagnostic = service.validate_strategy(preset.payload)
        assert diagnostic.strategy is not None
        run = service.run_strategy(diagnostic.strategy, token=preset.key)
        assert run.status is PortfolioRunStatus.UNAVAILABLE and run.failure_reason
        assert run.final_nav is None and not run.equity
        assert "Research status: SUCCESS" not in DemoService.report_html(run)

    # 7. Saved runs are all still readable and the evidence database was not altered by research.
    assert len(service.list_runs()) == 1 + 4
    assert database_digest(database) == created.digest
