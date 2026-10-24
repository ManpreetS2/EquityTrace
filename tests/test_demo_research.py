"""Offline synthetic demo regressions for the v1.0 release candidate."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

from equitytrace.database import initialize_database
from equitytrace.portfolio.models import BacktestResult, PortfolioRunStatus
from equitytrace.reporting.html import render_research_report
from equitytrace.strategy.models import fingerprint_request
from helpers.portfolio_fixtures import explicit_request, run_backtest
from test_portfolio_backtest import D0, D2, D3, _seed_two_name_path

ROOT = Path(__file__).resolve().parents[1]


def _load_demo_module():
    path = ROOT / "scripts" / "demo_research.py"
    spec = importlib.util.spec_from_file_location("equitytrace_demo_research", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_offline_demo_succeeds_and_is_economically_reproducible(tmp_path: Path) -> None:
    demo = _load_demo_module()
    out_a = tmp_path / "demo-a"
    out_b = tmp_path / "demo-b"
    summary_a = demo.run_demo(output_dir=out_a)
    summary_b = demo.run_demo(output_dir=out_b)

    assert summary_a["status"] == PortfolioRunStatus.SUCCESS.value
    assert summary_b["status"] == PortfolioRunStatus.SUCCESS.value
    assert summary_a["fingerprint"] == summary_b["fingerprint"]
    assert int(summary_a["successful_rebalances"]) >= 1
    assert int(summary_a["equity_points"]) >= 1
    assert int(summary_a["transition_rows"]) >= 1
    assert int(summary_a["signal_rows"]) >= 1
    assert summary_a["final_nav"] is not None
    assert "provider_adjusted_history_not_vintage_pit" in summary_a["warnings"]

    report_a = Path(str(summary_a["report_path"])).read_text(encoding="utf-8")
    report_b = Path(str(summary_b["report_path"])).read_text(encoding="utf-8")
    assert str(summary_a["fingerprint"]) in report_a
    assert str(summary_b["fingerprint"]) in report_b
    assert "Research status: SUCCESS" in report_a
    assert "SYNTHETIC DEMO ONLY" in report_a and "SYNTHETIC DEMO ONLY" in report_b
    assert "not historical investment performance" in report_a
    assert (out_a / "SYNTHETIC_DEMO.txt").read_text(encoding="utf-8").startswith("SYNTHETIC")
    assert "Research status: SUCCESS" in report_b
    assert "AAA" in report_a and "BBB" in report_a
    assert "Benchmark NAV" in report_a or "benchmark" in report_a.lower()
    assert "Drawdown" in report_a
    strategy_text = (ROOT / "examples" / "demo" / "strategy.json").read_text(encoding="utf-8")
    assert "SYNTHETIC DEMO ONLY" in strategy_text

    req_a = json.loads(Path(str(summary_a["request_json"])).read_text(encoding="utf-8"))
    req_b = json.loads(Path(str(summary_b["request_json"])).read_text(encoding="utf-8"))
    assert (
        req_a["export_metadata"]["economic_fingerprint"]
        == req_b["export_metadata"]["economic_fingerprint"]
    )
    assert req_a["request"]["factor"] == req_b["request"]["factor"]
    assert sorted(req_a["request"]["tickers"]) == sorted(req_b["request"]["tickers"])

    res_a = json.loads(Path(str(summary_a["result_json"])).read_text(encoding="utf-8"))
    res_b = json.loads(Path(str(summary_b["result_json"])).read_text(encoding="utf-8"))
    assert res_a["result"]["status"] == res_b["result"]["status"] == "success"
    assert (
        res_a["result"]["metrics"]["successful_rebalance_count"]
        == res_b["result"]["metrics"]["successful_rebalance_count"]
    )
    assert abs(float(res_a["result"]["final_nav"]) - float(res_b["result"]["final_nav"])) < 1e-12
    assert len(res_a["result"]["equity"]) == len(res_b["result"]["equity"])
    for left, right in zip(res_a["result"]["equity"], res_b["result"]["equity"], strict=True):
        assert abs(float(left["nav"]) - float(right["nav"])) < 1e-12
        assert abs(float(left["drawdown"]) - float(right["drawdown"])) < 1e-12
        if left["benchmark_nav"] is None:
            assert right["benchmark_nav"] is None
        else:
            assert abs(float(left["benchmark_nav"]) - float(right["benchmark_nav"])) < 1e-12

    export_repeat = out_a / "export_repeat"
    assert (export_repeat / "request.json").read_bytes() == Path(
        str(summary_a["request_json"])
    ).read_bytes()
    assert (export_repeat / "result.json").read_bytes() == Path(
        str(summary_a["result_json"])
    ).read_bytes()


def test_demo_html_and_json_byte_identical_for_one_persisted_run(tmp_path: Path) -> None:
    demo = _load_demo_module()
    summary = demo.run_demo(output_dir=tmp_path / "once")
    html_path = Path(str(summary["report_path"]))
    first = html_path.read_bytes()
    payload = json.loads(Path(str(summary["result_json"])).read_text(encoding="utf-8"))
    loaded = BacktestResult.model_validate(payload["result"])
    second = demo._label_report(render_research_report(loaded)).encode("utf-8")
    third = demo._label_report(render_research_report(loaded)).encode("utf-8")
    assert second == third
    assert first == second
    assert fingerprint_request(loaded.request) == summary["fingerprint"]
    req_bytes = Path(str(summary["request_json"])).read_bytes()
    res_bytes = Path(str(summary["result_json"])).read_bytes()
    assert (tmp_path / "once" / "export_repeat" / "request.json").read_bytes() == req_bytes
    assert (tmp_path / "once" / "export_repeat" / "result.json").read_bytes() == res_bytes


def test_failed_persisted_research_is_not_shown_as_success() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = initialize_database(tmp / "fail.duckdb")
    _seed_two_name_path(db, skip_bbb=D2)
    failed = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0,), top_n=2),
    )
    assert failed.status is PortfolioRunStatus.FAILED
    html = render_research_report(failed)
    assert "Research status: FAILED" in html
    assert "Research status: SUCCESS" not in html
    assert "held_return_missing" in html
    assert "Performance metrics are unavailable for this non-successful" in html
    assert 'class="metric-card"' not in html
