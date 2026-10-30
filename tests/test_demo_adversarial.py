"""Adversarial inputs against the supported research workflow (offline, deterministic)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest
from pydantic import ValidationError

from equitytrace.demo.prepare import DemoPaths, DemoPreparationError, prepare_demo
from equitytrace.demo.service import DemoService
from equitytrace.models import FinancialFact
from equitytrace.reporting.export import ReportingError, export_run_json, write_html_report


@pytest.fixture
def service(demo_dir: Path) -> DemoService:
    return DemoService(demo_dir)


def _run(service: DemoService, token: str = "adv"):  # type: ignore[no-untyped-def]
    outcome = service.validate_strategy(service.presets()[0].payload)
    assert outcome.strategy is not None
    return service.run_strategy(outcome.strategy, token=token)


# ------------------------------------------------------ exact availability boundary
def test_a_filing_is_invisible_one_microsecond_early_and_visible_at_its_instant(
    service: DemoService,
) -> None:
    accepted = datetime(2024, 3, 14, 20, 30, tzinfo=UTC)  # 16:30 EDT, SYBR FY2023 10-K
    for as_of, expected in (
        (accepted - timedelta(microseconds=1), None),
        (accepted, 3250 * 1_000_000.0),
        (accepted + timedelta(microseconds=1), 3250 * 1_000_000.0),
    ):
        periods = service.available_periods("SYBR", as_of)
        assert (periods[0].fiscal_year == 2023) is (expected is not None), as_of
        view = service.snapshot_view("SYBR", 2023, as_of, with_factors=False)
        revenue = view.snapshot.get_value("revenue")
        assert revenue is not None and revenue.value == expected, as_of


def test_naive_and_foreign_zone_as_of_values_are_interpreted_consistently(
    service: DemoService,
) -> None:
    utc = datetime(2024, 3, 14, 20, 30, tzinfo=UTC)
    eastern_local = utc.astimezone(ZoneInfo("America/New_York"))
    assert eastern_local == utc
    a = service.snapshot_view("SYBR", 2023, utc, with_factors=False).snapshot
    b = service.snapshot_view("SYBR", 2023, eastern_local, with_factors=False).snapshot
    assert a.income_statement == b.income_statement


def test_nonfinite_financial_values_cannot_enter_the_evidence_model() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            FinancialFact(
                cik="0000000001",
                taxonomy="us-gaap",
                concept="Assets",
                unit="USD",
                value=bad,
                available_at=datetime(2024, 1, 1, tzinfo=UTC),
            )


# ---------------------------------------------------------- invalid export targets
def test_exports_refuse_bad_destinations_and_never_clobber_existing_files(
    service: DemoService, tmp_path: Path
) -> None:
    result = _run(service)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("keep me", encoding="utf-8")
    with pytest.raises(ReportingError, match="not a directory"):
        export_run_json(result, blocker)
    assert blocker.read_text(encoding="utf-8") == "keep me"

    out = tmp_path / "export"
    request_path, result_path = export_run_json(result, out)
    first = (request_path.read_bytes(), result_path.read_bytes())
    with pytest.raises(ReportingError, match="already exists"):
        export_run_json(result, out)  # no silent overwrite without force
    assert (request_path.read_bytes(), result_path.read_bytes()) == first
    export_run_json(result, out, force=True)
    assert (request_path.read_bytes(), result_path.read_bytes()) == first  # deterministic

    with pytest.raises(ReportingError, match="directory"):
        write_html_report(tmp_path, DemoService.report_html(result))
    leftovers = [p.name for p in tmp_path.rglob("*.tmp")]
    assert leftovers == []


# --------------------------------------------------- invalid / corrupted stored state
def test_a_corrupted_database_file_is_reported_not_trusted_and_never_overwritten(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "demo"
    prepare_demo(directory)
    paths = DemoPaths(directory)
    paths.database.write_bytes(b"this is not a duckdb file")
    service = DemoService(directory)
    status = service.status()
    assert status.prepared is False and "cannot be read" in status.message
    assert service.list_runs() == [] and service.load_run("0" * 32) is None
    with pytest.raises(DemoPreparationError, match="discard saved demo runs"):
        service.prepare()
    assert paths.database.read_bytes() == b"this is not a duckdb file"  # untouched
    assert service.prepare(overwrite=True).action == "replaced"  # explicit consent rebuilds
    assert DemoService(directory).status().prepared is True


def test_a_missing_manifest_marks_the_demo_unprepared_even_if_the_database_exists(
    demo_dir: Path,
) -> None:
    DemoPaths(demo_dir).manifest.unlink()
    assert DemoService(demo_dir).status().prepared is False


def test_a_saved_run_with_corrupted_json_fails_closed_without_a_traceback(
    service: DemoService, demo_dir: Path
) -> None:
    good = _run(service, "good")
    bad = _run(service, "bad-token")
    conn = duckdb.connect(str(DemoPaths(demo_dir).database))
    conn.execute(
        "UPDATE portfolio_runs SET request_json = '{not json' WHERE run_id = ?", [bad.run_id]
    )
    conn.close()
    assert service.load_run(bad.run_id) is None  # unreadable run: unavailable, not a crash
    assert service.load_run(good.run_id) == good  # neighbours are unaffected
    summaries = {s.run_id: s for s in service.list_runs()}
    assert summaries[bad.run_id].fingerprint is None
    assert summaries[good.run_id].fingerprint is not None


def test_ui_survives_a_corrupted_saved_run(
    service: DemoService, demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamlit.testing.v1 import AppTest

    run = _run(service, "ui-bad")
    conn = duckdb.connect(str(DemoPaths(demo_dir).database))
    conn.execute("UPDATE portfolio_runs SET request_json = 'null' WHERE run_id = ?", [run.run_id])
    conn.close()
    monkeypatch.setenv("EQUITYTRACE_DEMO_DIR", str(demo_dir))
    root = Path(__file__).resolve().parents[1]
    app = AppTest.from_file(str(root / "src/equitytrace/ui/app.py"), default_timeout=120).run()
    for screen in ("Results", "Inspect decisions", "Reports & exports"):
        app.session_state["screen"] = screen
        app.run()
        assert not app.exception, (screen, [e.value for e in app.exception])
        assert "could not be loaded" in "\n".join(e.value for e in app.error)
