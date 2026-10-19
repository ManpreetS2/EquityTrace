"""Standalone HTML reports and deterministic JSON exports (v1.0 Milestone 2)."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import Database, initialize_database
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.portfolio.models import (
    ADJUSTED_HISTORY_WARNING,
    SURVIVORSHIP_WARNING,
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    LeakageAuditResult,
    PerformanceMetrics,
    PortfolioBaseline,
    PortfolioRunStatus,
    PortfolioSchedule,
    RebalanceRecord,
    RebalanceStatus,
    WeightTransition,
)
from equitytrace.reporting.export import (
    export_run_json,
    render_request_export,
    render_result_export,
)
from equitytrace.reporting.html import render_research_report
from equitytrace.repositories.portfolio import PortfolioRepository
from equitytrace.strategy.models import fingerprint_request
from test_portfolio_backtest import D0, D1, D2, D3, _seed_two_name_path
from test_research_cli import _prepare, _write_strategy

runner = CliRunner()


def _run_success(tmp_path: Path, monkeypatch) -> tuple[Path, BacktestResult]:  # type: ignore[no-untyped-def]
    db_path, strategy_path = _prepare(tmp_path, monkeypatch)
    from equitytrace.strategy.io import load_strategy

    request = load_strategy(strategy_path).to_backtest_request()
    db = initialize_database(db_path)
    with db.session() as conn:
        result = BacktestEngine(conn).run(request)
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert loaded.status is PortfolioRunStatus.SUCCESS
    return db_path, loaded


def _crafted_result(
    *,
    status: PortfolioRunStatus,
    failure_reason: str | None = None,
    warnings: tuple[str, ...] = (),
    metrics: PerformanceMetrics | None = None,
    equity: tuple[EquityPoint, ...] = (),
    rebalances: tuple[RebalanceRecord, ...] = (),
    audit: LeakageAuditResult | None = None,
    final_nav: float | None = None,
) -> BacktestResult:
    request = BacktestRequest(
        tickers=("AAA", "BBB"),
        factor="roa",
        top_n=2,
        baseline=PortfolioBaseline.EQUAL_WEIGHT,
        start_date=D0,
        end_date=D3,
        schedule=PortfolioSchedule.EXPLICIT,
        explicit_dates=(D0, D1),
    )
    return BacktestResult(
        run_id="deadbeefcafebabe",
        status=status,
        request=request,
        failure_reason=failure_reason,
        warnings=warnings,
        created_at=datetime(2024, 1, 2, 16, 15, tzinfo=UTC),
        completed_at=datetime(2024, 1, 2, 16, 16, tzinfo=UTC),
        universe=("AAA", "BBB"),
        final_nav=final_nav,
        metrics=metrics,
        rebalances=rebalances,
        equity=equity,
        audit=audit,
        package_version="1.0.0.dev0",
    )


def test_research_help_lists_report_and_export() -> None:
    result = runner.invoke(app, ["research", "--help"])
    assert result.exit_code == 0
    for command in ("validate", "run", "show", "report", "export"):
        assert command in result.output


def test_portfolio_cli_still_works() -> None:
    result = runner.invoke(app, ["portfolio", "--help"])
    assert result.exit_code == 0
    assert "backtest" in result.output


def test_success_report_identity_metrics_and_charts(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    _db_path, loaded = _run_success(tmp_path, monkeypatch)
    digest = fingerprint_request(loaded.request)
    html = render_research_report(loaded)
    assert loaded.run_id in html
    assert digest in html
    assert loaded.package_version in html
    assert "Research status: SUCCESS" in html
    assert "Research status: FAILED" not in html
    assert "Research status: UNAVAILABLE" not in html
    assert loaded.metrics is not None
    assert f"{loaded.metrics.gross_turnover:.6g}" in html
    assert f"{loaded.metrics.transaction_cost_total:.6g}" in html
    assert str(loaded.metrics.successful_rebalance_count) in html
    assert 'aria-label="Portfolio and benchmark NAV"' in html
    assert 'aria-label="Portfolio drawdown"' in html
    assert "polyline" in html
    assert "provider_adjusted_history_not_vintage_pit" in html
    assert "survivorship_bias_possible" in html
    assert ADJUSTED_HISTORY_WARNING in html
    assert SURVIVORSHIP_WARNING in html
    # Rebalance timestamps and turnover/cost from persisted rows.
    first = loaded.rebalances[0]
    assert first.decision_at.isoformat() in html or first.decision_at.isoformat().replace(
        "+00:00", "+00:00"
    )
    assert f"{first.gross_turnover:.6g}" in html
    assert f"{first.cost_amount:.6g}" in html
    # Weight transitions and signal evidence.
    transition = first.transitions[0]
    assert transition.symbol in html
    assert f"{transition.current_weight_before:.6g}" in html
    assert f"{transition.target_weight_after:.6g}" in html
    if transition.signal_rank is not None:
        assert str(transition.signal_rank) in html
    if transition.signal_period:
        assert transition.signal_period in html
    if transition.signal_provenance_json:
        assert "provenance" in html.lower()
        # Provenance content retained (escaped).
        payload = json.loads(transition.signal_provenance_json)
        if "ticker" in payload:
            assert str(payload["ticker"]) in html
    assert "Target weights" in html or "Weight transitions" in html
    assert "Orders filled" not in html
    assert "Executed shares" not in html


def test_benchmark_only_with_valid_data() -> None:
    with_bench = _crafted_result(
        status=PortfolioRunStatus.SUCCESS,
        final_nav=1.0,
        metrics=PerformanceMetrics(successful_rebalance_count=0),
        equity=(
            EquityPoint(
                valuation_at=datetime(2024, 1, 2, 21, 15, tzinfo=UTC),
                nav=1.0,
                cash_weight=0.0,
                drawdown=0.0,
                benchmark_nav=100.0,
            ),
            EquityPoint(
                valuation_at=datetime(2024, 1, 3, 21, 15, tzinfo=UTC),
                nav=1.1,
                cash_weight=0.0,
                drawdown=0.0,
                benchmark_nav=101.0,
            ),
        ),
    )
    html = render_research_report(with_bench)
    assert "Benchmark NAV (stored values only)" in html
    assert "stroke-dasharray" in html

    without = _crafted_result(
        status=PortfolioRunStatus.SUCCESS,
        final_nav=1.0,
        metrics=PerformanceMetrics(),
        equity=(
            EquityPoint(
                valuation_at=datetime(2024, 1, 2, 21, 15, tzinfo=UTC),
                nav=1.0,
                cash_weight=1.0,
                drawdown=0.0,
                benchmark_nav=None,
            ),
        ),
    )
    html2 = render_research_report(without)
    assert "Benchmark unavailable" in html2


def test_empty_equity_does_not_fabricate_charts() -> None:
    result = _crafted_result(
        status=PortfolioRunStatus.SUCCESS,
        final_nav=1.0,
        metrics=PerformanceMetrics(),
        equity=(),
    )
    html = render_research_report(result)
    assert "no persisted equity points" in html
    assert 'aria-label="Portfolio and benchmark NAV"' not in html


def test_unavailable_and_failed_reports_do_not_imply_success() -> None:
    unavail = _crafted_result(
        status=PortfolioRunStatus.UNAVAILABLE,
        failure_reason="factor_universe_too_small",
        warnings=(ADJUSTED_HISTORY_WARNING,),
        audit=LeakageAuditResult(passed=True, failures=()),
        metrics=PerformanceMetrics(
            gross_turnover=0.0,
            transaction_cost_total=0.0,
            successful_rebalance_count=0,
        ),
        final_nav=None,
    )
    failed = _crafted_result(
        status=PortfolioRunStatus.FAILED,
        failure_reason="leakage_detected",
        warnings=(SURVIVORSHIP_WARNING,),
        audit=LeakageAuditResult(passed=False, failures=("future_signal",)),
        equity=(),
        metrics=PerformanceMetrics(
            gross_turnover=0.0,
            transaction_cost_total=0.0,
            successful_rebalance_count=0,
        ),
        final_nav=None,
    )
    html_u = render_research_report(unavail)
    html_f = render_research_report(failed)
    assert "Research status: UNAVAILABLE" in html_u
    assert "Research status: SUCCESS" not in html_u
    assert "factor_universe_too_small" in html_u
    assert ADJUSTED_HISTORY_WARNING in html_u
    assert "Performance metrics are unavailable for this non-successful" in html_u
    assert 'class="metric-grid"' not in html_u
    assert 'class="metric-card"' not in html_u
    assert "Final NAV" not in html_u
    assert "Research status: FAILED" in html_f
    assert "Research status: SUCCESS" not in html_f
    assert "leakage_detected" in html_f
    assert "future_signal" in html_f
    assert "Audit passed: <strong>False</strong>" in html_f
    assert "Performance metrics are unavailable for this non-successful" in html_f
    assert 'class="metric-grid"' not in html_f
    assert 'class="metric-card"' not in html_f
    assert "0.0000%" not in html_f
    assert "no persisted equity points" in html_f


def test_malicious_strings_are_escaped() -> None:
    evil = '<script>alert("xss")</script>&<>"'
    result = _crafted_result(
        status=PortfolioRunStatus.FAILED,
        failure_reason=evil,
        warnings=(evil,),
        audit=LeakageAuditResult(passed=False, failures=(evil,)),
        rebalances=(
            RebalanceRecord(
                decision_at=datetime(2024, 1, 2, 21, 15, tzinfo=UTC),
                target_effective_at=datetime(2024, 1, 3, 21, 15, tzinfo=UTC),
                status=RebalanceStatus.FAILED,
                reason=evil,
                pre_cost_nav=1.0,
                post_cost_nav=1.0,
                gross_turnover=0.0,
                cost_amount=0.0,
                transitions=(
                    WeightTransition(
                        symbol=evil,
                        instrument_id="inst",
                        current_weight_before=0.0,
                        target_weight_after=1.0,
                        delta_weight=1.0,
                        gross_notional=1.0,
                        allocated_cost=0.0,
                        signal_value=0.1,
                        signal_rank=1,
                        signal_period=evil,
                        signal_provenance_json=json.dumps(
                            {"ticker": evil, "accession": evil},
                            sort_keys=True,
                        ),
                    ),
                ),
            ),
        ),
    )
    html = render_research_report(result)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "alert(" in html  # escaped text still visible
    assert evil not in html


def test_html_and_json_are_deterministic(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    _db_path, loaded = _run_success(tmp_path, monkeypatch)
    html_a = render_research_report(loaded)
    html_b = render_research_report(loaded)
    assert html_a == html_b
    req_a = render_request_export(loaded)
    req_b = render_request_export(loaded)
    res_a = render_result_export(loaded)
    res_b = render_result_export(loaded)
    assert req_a == req_b
    assert res_a == res_b
    assert req_a.endswith("\n")
    assert res_a.endswith("\n")


def test_export_roundtrip_and_fingerprint(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    _db_path, loaded = _run_success(tmp_path, monkeypatch)
    out = tmp_path / "exports"
    request_path, result_path = export_run_json(loaded, out, force=False)
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    digest = fingerprint_request(loaded.request)
    assert request_payload["export_metadata"]["economic_fingerprint"] == digest
    assert result_payload["export_metadata"]["economic_fingerprint"] == digest
    assert request_payload["request"]["factor"] == loaded.request.factor
    assert result_payload["result"]["run_id"] == loaded.run_id
    assert result_payload["result"]["status"] == loaded.status.value
    assert result_payload["result"]["package_version"] == loaded.package_version
    # Roundtrip request economic fields through BacktestRequest.
    BacktestRequest.model_validate(request_payload["request"])
    BacktestResult.model_validate(result_payload["result"])


def test_overwrite_protection_and_force(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    db_path, loaded = _run_success(tmp_path, monkeypatch)
    report = tmp_path / "out" / "report.html"
    first = runner.invoke(
        app,
        [
            "research",
            "report",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(report),
        ],
    )
    assert first.exit_code == 0, first.output
    assert report.exists()
    blocked = runner.invoke(
        app,
        [
            "research",
            "report",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(report),
        ],
    )
    assert blocked.exit_code == 1
    assert "already exists" in blocked.output
    forced = runner.invoke(
        app,
        [
            "research",
            "report",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(report),
            "--force",
        ],
    )
    assert forced.exit_code == 0, forced.output

    export_dir = tmp_path / "json"
    exp1 = runner.invoke(
        app,
        [
            "research",
            "export",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(export_dir),
        ],
    )
    assert exp1.exit_code == 0, exp1.output
    exp_block = runner.invoke(
        app,
        [
            "research",
            "export",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(export_dir),
        ],
    )
    assert exp_block.exit_code == 1
    exp_force = runner.invoke(
        app,
        [
            "research",
            "export",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(export_dir),
            "--force",
        ],
    )
    assert exp_force.exit_code == 0


def test_missing_run_and_readonly_database(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    db_path, _ = _prepare(tmp_path, monkeypatch)
    opened: list[bool] = []
    real_connect = Database.connect

    def _track(self: Database, *, read_only: bool = False) -> object:
        opened.append(read_only)
        return real_connect(self, read_only=read_only)

    monkeypatch.setattr(Database, "connect", _track)
    missing = runner.invoke(
        app,
        [
            "research",
            "report",
            "missing-run",
            "--database",
            str(db_path),
            "--output",
            str(tmp_path / "x.html"),
        ],
    )
    assert missing.exit_code == 1
    assert "No portfolio run found" in missing.output
    assert opened == [True]

    opened.clear()
    missing_export = runner.invoke(
        app,
        [
            "research",
            "export",
            "missing-run",
            "--database",
            str(db_path),
            "--output",
            str(tmp_path / "exp"),
        ],
    )
    assert missing_export.exit_code == 1
    assert opened == [True]


def test_cli_report_export_success_and_failed_diagnostic(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    db_path, loaded = _run_success(tmp_path, monkeypatch)
    report = tmp_path / "ok.html"
    result = runner.invoke(
        app,
        [
            "research",
            "report",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "research_status=success" in result.output
    assert report.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    # Failed diagnostic still exits 0 for report generation.
    fail_db = tmp_path / "fail.duckdb"
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(fail_db))
    clear_settings_cache()
    db = initialize_database(fail_db)
    _seed_two_name_path(db, skip_bbb=D2)
    fail_strategy = _write_strategy(
        tmp_path / "fail.json",
        explicit_dates=[D0.isoformat()],
    )
    fail_run = runner.invoke(
        app,
        ["research", "run", "--strategy", str(fail_strategy), "--database", str(fail_db)],
    )
    assert fail_run.exit_code == 1
    with db.session() as conn:
        fail_id = str(
            conn.execute(
                "SELECT run_id FROM portfolio_runs WHERE status = 'failed' LIMIT 1"
            ).fetchone()[0]
        )
    fail_report = tmp_path / "fail.html"
    reported = runner.invoke(
        app,
        [
            "research",
            "report",
            fail_id,
            "--database",
            str(fail_db),
            "--output",
            str(fail_report),
        ],
    )
    assert reported.exit_code == 0, reported.output
    assert "research_status=failed" in reported.output
    body = fail_report.read_text(encoding="utf-8")
    assert "Research status: FAILED" in body
    assert "Research status: SUCCESS" not in body


def test_report_does_not_open_network(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    db_path, loaded = _run_success(tmp_path, monkeypatch)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("report/export must not call the network")

    monkeypatch.setattr("urllib.request.urlopen", _refuse)
    html_path = tmp_path / "offline.html"
    result = runner.invoke(
        app,
        [
            "research",
            "report",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(html_path),
        ],
    )
    assert result.exit_code == 0, result.output
    export = runner.invoke(
        app,
        [
            "research",
            "export",
            loaded.run_id,
            "--database",
            str(db_path),
            "--output",
            str(tmp_path / "offline-json"),
        ],
    )
    assert export.exit_code == 0, export.output


def test_null_metrics_show_unavailable_not_zero() -> None:
    result = _crafted_result(
        status=PortfolioRunStatus.UNAVAILABLE,
        failure_reason="calendar_missing",
        metrics=PerformanceMetrics(
            total_return=None,
            annualized_return=None,
            annualized_volatility=None,
            sharpe_ratio=None,
            max_drawdown=None,
            gross_turnover=0.0,
            transaction_cost_total=0.0,
            successful_rebalance_count=0,
        ),
        final_nav=None,
    )
    html = render_research_report(result)
    assert "Performance metrics are unavailable for this non-successful" in html
    assert 'class="metric-grid"' not in html
    assert 'class="metric-card"' not in html
    assert "Final NAV" not in html
    assert "0.0000%" not in html
    assert "Gross turnover" not in html


def test_report_determinism_under_hash_seed(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Included in the PYTHONHASHSEED CI subset."""
    _db_path, loaded = _run_success(tmp_path, monkeypatch)
    assert render_research_report(loaded) == render_research_report(loaded)
    assert render_request_export(loaded) == render_request_export(loaded)
    assert render_result_export(loaded) == render_result_export(loaded)


def test_timestamps_are_utc_and_timezone_independent(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    import time

    _db_path, loaded = _run_success(tmp_path, monkeypatch)
    expected_created = loaded.created_at.astimezone(UTC).isoformat()
    assert expected_created.endswith("+00:00")

    html_default = render_research_report(loaded)
    assert expected_created in html_default

    previous_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        html_west = render_research_report(loaded)
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        html_east = render_research_report(loaded)
        monkeypatch.setenv("TZ", "UTC")
        time.tzset()
        html_utc = render_research_report(loaded)
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()

    assert html_west == html_east == html_utc == html_default
    assert expected_created in html_west
    assert "-07:00" not in html_west
    assert "-08:00" not in html_west
    assert "+09:00" not in html_west
    # Decision timestamps in the rebalance table also stay UTC.
    assert loaded.rebalances
    decision = loaded.rebalances[0].decision_at.astimezone(UTC).isoformat()
    assert decision.endswith("+00:00")
    assert decision in html_west


def test_persisted_failed_and_unavailable_reports(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    from helpers.portfolio_fixtures import explicit_request, run_backtest

    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    clear_settings_cache()

    fail_db_path = tmp_path / "persisted-fail.duckdb"
    fail_db = initialize_database(fail_db_path)
    _seed_two_name_path(fail_db, skip_bbb=D2)
    failed = run_backtest(
        fail_db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert failed.status is PortfolioRunStatus.FAILED
    assert failed.failure_reason == "held_return_missing"
    with fail_db.session(read_only=True) as conn:
        loaded_fail = PortfolioRepository(conn).get_run(failed.run_id)
    assert loaded_fail is not None
    html_fail = render_research_report(loaded_fail)
    assert "Research status: FAILED" in html_fail
    assert "Research status: SUCCESS" not in html_fail
    assert "held_return_missing" in html_fail
    assert "Performance metrics are unavailable for this non-successful" in html_fail
    assert 'class="metric-grid"' not in html_fail
    assert 'class="metric-card"' not in html_fail
    assert "Gross turnover" not in html_fail
    assert ADJUSTED_HISTORY_WARNING in html_fail
    # JSON export still preserves the stored metrics object (including zeros).
    export_payload = json.loads(render_result_export(loaded_fail))
    assert export_payload["result"]["status"] == "failed"
    assert export_payload["result"]["failure_reason"] == "held_return_missing"
    assert export_payload["result"]["metrics"] is not None
    assert export_payload["result"]["metrics"]["gross_turnover"] == 0.0

    unavail_db = initialize_database(tmp_path / "persisted-unavail.duckdb")
    _seed_two_name_path(unavail_db)
    unavailable = run_backtest(
        unavail_db,
        explicit_request(("AAA",), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert unavailable.status is PortfolioRunStatus.UNAVAILABLE
    assert unavailable.failure_reason == "factor_universe_too_small"
    with unavail_db.session(read_only=True) as conn:
        loaded_unavail = PortfolioRepository(conn).get_run(unavailable.run_id)
    assert loaded_unavail is not None
    html_u = render_research_report(loaded_unavail)
    assert "Research status: UNAVAILABLE" in html_u
    assert "Research status: SUCCESS" not in html_u
    assert "factor_universe_too_small" in html_u
    assert "Performance metrics are unavailable for this non-successful" in html_u
    assert 'class="metric-grid"' not in html_u
    assert 'class="metric-card"' not in html_u


def test_chart_gaps_do_not_connect_missing_observations() -> None:
    equity = (
        EquityPoint(
            valuation_at=datetime(2024, 1, 2, 21, 15, tzinfo=UTC),
            nav=1.0,
            cash_weight=0.0,
            drawdown=0.0,
            benchmark_nav=100.0,
        ),
        EquityPoint(
            valuation_at=datetime(2024, 1, 3, 21, 15, tzinfo=UTC),
            nav=float("nan"),
            cash_weight=0.0,
            drawdown=float("nan"),
            benchmark_nav=None,
        ),
        EquityPoint(
            valuation_at=datetime(2024, 1, 4, 21, 15, tzinfo=UTC),
            nav=1.05,
            cash_weight=0.0,
            drawdown=-0.01,
            benchmark_nav=102.0,
        ),
        EquityPoint(
            valuation_at=datetime(2024, 1, 5, 21, 15, tzinfo=UTC),
            nav=1.06,
            cash_weight=0.0,
            drawdown=-0.02,
            benchmark_nav=float("inf"),
        ),
    )
    result = _crafted_result(
        status=PortfolioRunStatus.SUCCESS,
        final_nav=1.06,
        metrics=PerformanceMetrics(successful_rebalance_count=1),
        equity=equity,
    )
    html = render_research_report(result)
    assert "trading-session sequence" in html
    assert "gaps are unconnected" in html
    # Portfolio NAV: finite sessions 0 and 2-3 => one circle + one polyline.
    # A single continuous polyline would incorrectly bridge the NaN gap.
    nav_section_start = html.index('aria-label="Portfolio and benchmark NAV"')
    nav_section_end = html.index("</svg>", nav_section_start) + len("</svg>")
    nav_svg = html[nav_section_start:nav_section_end]
    portfolio_polylines = re.findall(r"<polyline[^>]*stroke=\"#1f4e79\"[^>]*>", nav_svg)
    portfolio_circles = re.findall(r"<circle[^>]*fill=\"#1f4e79\"[^>]*>", nav_svg)
    assert len(portfolio_polylines) + len(portfolio_circles) == 2
    # Benchmark: sessions 0 and 2 only (None and +inf are gaps) => two circles.
    bench_polylines = re.findall(r"<polyline[^>]*stroke=\"#6b7280\"[^>]*>", nav_svg)
    bench_circles = re.findall(r"<circle[^>]*fill=\"#6b7280\"[^>]*>", nav_svg)
    assert len(bench_polylines) + len(bench_circles) == 2
    assert len(bench_circles) == 2
    # No invented forward-filled benchmark mid-gap point.
    assert "101" not in nav_svg
    # Drawdown also splits at the NaN observation.
    dd_start = html.index('aria-label="Portfolio drawdown"')
    dd_end = html.index("</svg>", dd_start) + len("</svg>")
    dd_svg = html[dd_start:dd_end]
    dd_polylines = re.findall(r"<polyline[^>]*stroke=\"#8b3a3a\"[^>]*>", dd_svg)
    dd_circles = re.findall(r"<circle[^>]*fill=\"#8b3a3a\"[^>]*>", dd_svg)
    assert len(dd_polylines) + len(dd_circles) == 2
