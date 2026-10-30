"""Whole-workflow integration: demo service ↔ existing CLI, scripts and exports."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.demo.prepare import DemoPaths
from equitytrace.demo.service import DemoService, build_presets
from equitytrace.portfolio.models import PortfolioRunStatus
from equitytrace.reporting.html import render_research_report
from equitytrace.strategy.models import fingerprint_request

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ROOT / "examples" / "demo" / "strategies"
runner = CliRunner()


def _load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"equitytrace_script_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    clear_settings_cache()


def _fingerprint_from(output: str) -> str:
    match = re.search(r"fingerprint=([0-9a-f]{64})", output.replace("\n", ""))
    assert match, output
    return match.group(1)


def test_example_strategy_files_match_presets_and_validate_in_the_cli(cli_env: None) -> None:
    service = DemoService(ROOT / "nonexistent-demo-dir")  # validation needs no database
    for preset in build_presets():
        path = STRATEGIES / f"{preset.key}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == preset.payload
        result = runner.invoke(app, ["research", "validate", "--strategy", str(path)])
        assert result.exit_code == 0, result.output
        expected = service.validate_strategy(preset.payload).fingerprint
        assert _fingerprint_from(result.output) == expected
    smoke = runner.invoke(
        app, ["research", "validate", "--strategy", str(ROOT / "examples/demo/strategy.json")]
    )
    assert smoke.exit_code == 0, smoke.output
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0 and "research" in help_result.output


def test_cli_and_demo_service_agree_on_the_same_persisted_research(
    demo_dir: Path, cli_env: None, tmp_path: Path
) -> None:
    database = DemoPaths(demo_dir).database
    strategy_path = STRATEGIES / "roa-equal-monthly.json"
    service = DemoService(demo_dir)

    run = runner.invoke(
        app,
        ["research", "run", "--strategy", str(strategy_path), "--database", str(database)],
    )
    assert run.exit_code == 0, run.output
    (summary,) = service.list_runs()
    cli_result = service.load_run(summary.run_id)
    assert cli_result is not None and cli_result.status is PortfolioRunStatus.SUCCESS

    validation = service.validate_strategy(build_presets()[0].payload)
    assert validation.strategy is not None
    ui_result = service.run_strategy(validation.strategy, token="ui-click")
    assert ui_result.run_id != cli_result.run_id  # distinct runs ...
    assert fingerprint_request(ui_result.request) == fingerprint_request(cli_result.request)
    assert ui_result.final_nav == pytest.approx(cli_result.final_nav or 0.0, abs=1e-12)
    assert ui_result.metrics == cli_result.metrics  # ... identical economics
    assert [r.model_dump() for r in ui_result.rebalances] == [
        r.model_dump() for r in cli_result.rebalances
    ]

    shown = runner.invoke(app, ["research", "show", ui_result.run_id, "--database", str(database)])
    assert shown.exit_code == 0, shown.output
    assert _fingerprint_from(shown.output) == fingerprint_request(ui_result.request)
    squashed = shown.output.replace("\n", "").replace(" ", "")
    assert ui_result.run_id in squashed and "status=success" in squashed

    export_dir = tmp_path / "export"
    exported = runner.invoke(
        app,
        [
            "research",
            "export",
            ui_result.run_id,
            "--output",
            str(export_dir),
            "--database",
            str(database),
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert (export_dir / "request.json").read_bytes() == DemoService.request_json(
        ui_result
    ).encode()
    assert (export_dir / "result.json").read_bytes() == DemoService.result_json(ui_result).encode()

    report_path = tmp_path / "report.html"
    reported = runner.invoke(
        app,
        [
            "research",
            "report",
            ui_result.run_id,
            "--output",
            str(report_path),
            "--database",
            str(database),
        ],
    )
    assert reported.exit_code == 0, reported.output
    cli_html = report_path.read_text(encoding="utf-8")
    assert cli_html == render_research_report(ui_result)
    labelled = DemoService.report_html(ui_result)
    assert "SYNTHETIC DEMO ONLY" in labelled and "SYNTHETIC DEMO ONLY" not in cli_html
    start = labelled.index('<p role="note"')
    end = labelled.index("</p>\n", start) + len("</p>\n")
    assert labelled[:start] + labelled[end:] == cli_html


def test_cli_reports_unavailable_demo_research_with_the_documented_exit_code(
    demo_dir: Path, cli_env: None
) -> None:
    database = DemoPaths(demo_dir).database
    path = STRATEGIES / "diag-share-classes.json"
    result = runner.invoke(
        app, ["research", "run", "--strategy", str(path), "--database", str(database)]
    )
    assert result.exit_code == 2, result.output
    squashed = result.output.replace("\n", "").replace(" ", "")
    assert "status=unavailable" in squashed
    assert "multiple_securities_same_issuer_unsupported" in squashed
    assert "Finalnav" not in squashed and "Totalreturn" not in squashed


def test_short_smoke_demo_no_longer_publishes_facts_before_the_period_ends(
    tmp_path: Path,
) -> None:
    """Regression: the old fixture made FY2023 facts public in June 2023."""
    demo = _load_script("demo_research")
    db = initialize_database(tmp_path / "smoke.duckdb")
    with db.session() as conn:
        demo.seed_synthetic_universe(conn)
        rows = conn.execute(
            "SELECT fiscal_year, end_date, filing_date, available_at, accession_number "
            "FROM financial_facts"
        ).fetchall()
    assert rows
    eastern = ZoneInfo("America/New_York")
    for fiscal_year, end_date, filing_date, available_at, accession in rows:
        assert end_date == date(fiscal_year, 12, 31), accession
        assert filing_date > end_date
        assert available_at > datetime.combine(end_date, time(23, 59, 59), tzinfo=eastern)
        assert available_at.year == fiscal_year + 1
    assert {row[0] for row in rows} == {2021, 2022}
    # The research window must not need anything later than FY2022.
    assert max(row[3] for row in rows) < datetime(2024, 1, 2, tzinfo=UTC)


def test_launcher_prepares_safely_and_binds_locally_only(tmp_path: Path) -> None:
    launcher = _load_script("launch_demo")
    directory = tmp_path / "demo"
    assert launcher.main(["--prepare-only", "--directory", str(directory)]) == 0
    assert DemoPaths(directory).database.exists()
    assert launcher.main(["--prepare-only", "--directory", str(directory)]) == 0
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    DemoPaths(blocked).database.write_bytes(b"not ours")
    assert launcher.main(["--prepare-only", "--directory", str(blocked)]) == 1
    assert DemoPaths(blocked).database.read_bytes() == b"not ours"

    command = launcher.build_command(port=8765, headless=True)
    assert command[:4] == [sys.executable, "-m", "streamlit", "run"]
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert command[command.index("--browser.gatherUsageStats") + 1] == "false"
    assert Path(command[4]) == ROOT / "src" / "equitytrace" / "ui" / "app.py"
    env = launcher.build_environment(directory)
    assert env["EQUITYTRACE_DEMO_DIR"] == str(directory)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT / "src")
