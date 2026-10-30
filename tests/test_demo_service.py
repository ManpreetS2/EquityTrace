"""UI-free demo service: validation, point-in-time reads, runs, persistence and exports."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from equitytrace.demo.service import (
    DemoNotPreparedError,
    DemoService,
    build_presets,
    decision_time,
)
from equitytrace.demo.views import (
    allocation_rows,
    decision_detail,
    equity_rows,
    metric_cells,
)
from equitytrace.portfolio.models import (
    BacktestResult,
    PortfolioRunStatus,
    RebalanceStatus,
)
from equitytrace.reporting.export import export_run_json
from equitytrace.reporting.html import render_research_report
from equitytrace.strategy.models import fingerprint_request

SUCCESS_KEYS = ("roa-equal-monthly", "growth-inverse-vol-quarterly", "margin-min-variance-monthly")
UNAVAILABLE = {
    "diag-share-classes": "multiple_securities_same_issuer_unsupported",
    "diag-valuation-no-market-cap": "factor_universe_too_small",
    "diag-short-price-history": "baseline_unavailable",
    "diag-evidence-not-public": "factor_universe_too_small",
}


def _payload(service: DemoService, key: str, **overrides: Any) -> dict[str, Any]:
    preset = next(p for p in service.presets() if p.key == key)
    return {**preset.payload, **overrides}


def _run(service: DemoService, key: str, token: str | None = None) -> BacktestResult:
    outcome = service.validate_strategy(_payload(service, key))
    assert outcome.ok and outcome.strategy is not None, outcome.errors
    return service.run_strategy(outcome.strategy, token=token or key)


@pytest.fixture
def service(demo_dir: Path) -> DemoService:
    return DemoService(demo_dir)


# ------------------------------------------------------------------ readiness
def test_unprepared_service_reports_honestly_and_reads_fail_closed(tmp_path: Path) -> None:
    service = DemoService(tmp_path / "nothing-here")
    status = service.status()
    assert status.prepared is False and "not been prepared" in status.message
    assert not (tmp_path / "nothing-here").exists()  # status never creates anything
    with pytest.raises(DemoNotPreparedError):
        service.securities()
    assert service.list_runs() == []
    assert service.load_run("0" * 32) is None


def test_prepared_status_and_security_listing(service: DemoService) -> None:
    status = service.status()
    assert status.prepared and status.fact_count == 168 and status.bar_count == 5670
    assert status.saved_run_count == 0 and len(status.tickers) == 8
    rows = {row.ticker: row for row in service.securities()}
    assert rows["SYGA"].cik == rows["SYGB"].cik and rows["SYGA"].securities_for_issuer == 2
    assert rows["SYAL"].securities_for_issuer == 1


# --------------------------------------------------------- point-in-time reads
def test_fiscal_year_is_invisible_before_its_filing_is_public(service: DemoService) -> None:
    # SYBR accepts its FY2023 10-K on 2024-03-14 at 16:30 ET, after the 16:15 decision time.
    for day, newest in (
        (date(2024, 3, 13), 2022),
        (date(2024, 3, 14), 2022),
        (date(2024, 3, 15), 2023),
    ):
        periods = service.available_periods("SYBR", decision_time(day))
        assert periods[0].fiscal_year == newest, day
    before = service.snapshot_view("SYBR", 2023, decision_time(date(2024, 3, 14)))
    assert before.snapshot.get_value("revenue") is not None
    revenue = before.snapshot.get_value("revenue")
    assert revenue is not None and revenue.value is None
    assert all(item.value is None for item in before.snapshot.income_statement.values())
    after = service.snapshot_view("SYBR", 2023, decision_time(date(2024, 3, 15)))
    revenue_after = after.snapshot.get_value("revenue")
    assert revenue_after is not None and revenue_after.value == 3250 * 1_000_000.0
    provenance = revenue_after.provenance
    assert provenance.accession_number == "0990000002-24-000001"
    assert provenance.available_at is not None
    assert provenance.available_at <= decision_time(date(2024, 3, 15))
    assert provenance.available_at > decision_time(date(2024, 3, 14))
    assert provenance.form == "10-K" and provenance.period_end == date(2023, 12, 31)


def test_period_end_is_not_availability(service: DemoService) -> None:
    for ticker in ("SYAL", "SYBR", "SYGA"):
        assert (
            service.available_periods(ticker, decision_time(date(2024, 1, 2)))[0].fiscal_year
            == 2022
        )
        assert service.available_periods(ticker, decision_time(date(2022, 1, 3))) == []


def test_filings_listing_marks_what_is_public(service: DemoService) -> None:
    rows = service.filings("SYBR", decision_time(date(2024, 3, 14)))
    assert [(r.report_date.year, r.public_at_as_of) for r in rows if r.report_date] == [
        (2021, True),
        (2022, True),
        (2023, False),
    ]


def test_ranking_uses_registry_direction_and_each_names_newest_public_year(
    service: DemoService,
) -> None:
    names = ["SYAL", "SYBR", "SYCD", "SYDL", "SYEN", "SYFM", "SYGA"]
    ranked = service.ranking_at(names, "roa", decision_time(date(2024, 2, 29)))
    valid = [item for item in ranked if item.result is not None]
    assert [item.rank for item in valid] == list(range(1, 8))
    values = [item.result.value for item in valid if item.result is not None]
    assert values == sorted(values, reverse=True)  # higher_is_better
    years = {item.result.period.fiscal_year for item in valid if item.result is not None}
    assert years == {2022, 2023}  # SYBR/SYFM/SYDL have not filed FY2023 by 2024-02-29
    by_name = {item.ticker: item for item in valid if item.result is not None}
    assert by_name["SYAL"].result is not None and by_name["SYAL"].result.period.fiscal_year == 2023
    assert by_name["SYBR"].result is not None and by_name["SYBR"].result.period.fiscal_year == 2022
    lower = service.ranking_at(names, "debt_change", decision_time(date(2024, 3, 29)))
    values = [item.result.value for item in lower if item.result and item.result.value is not None]
    assert values == sorted(values)  # lower_is_better ranks ascending


def test_valuation_factors_are_unavailable_with_a_reason_not_a_guess(service: DemoService) -> None:
    view = service.snapshot_view("SYAL", 2023, decision_time(date(2024, 3, 29)))
    valuation = {f.factor: f for f in view.factors if f.factor.startswith("price_to")}
    assert len(valuation) == 3
    for item in valuation.values():
        assert not item.valid and item.value is None and item.unavailable_reason
    assert {f.factor for f in view.factors if f.valid} >= {"roa", "revenue_growth"}


# ----------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"top_n": 0}, "top_n"),
        ({"top_n": 99}, "top_n must be <= number of unique universe tickers"),
        ({"start_date": "2024-06-01", "end_date": "2024-01-01"}, "start_date must be on or before"),
        ({"start_date": "2024-13-45"}, "start_date"),
        ({"factor": "astrology"}, "factor"),
        ({"baseline": "momentum_chasing"}, "baseline"),
        ({"tickers": []}, "tickers"),
        ({"schedule": "explicit", "explicit_dates": []}, "explicit schedule requires"),
        ({"schedule": "explicit", "explicit_dates": ["2030-01-01"]}, "outside"),
        ({"schedule": "monthly", "explicit_dates": ["2024-01-31"]}, "explicit_dates must be empty"),
        ({"cost_bps": -1}, "cost_bps"),
        ({"initial_nav": 0}, "initial_nav"),
        ({"adjustment_mode": "none"}, "adjustment_mode"),
        ({"name": "   "}, "name"),
        ({"surprise_field": 1}, "surprise_field"),
    ],
)
def test_invalid_strategies_are_rejected_before_any_engine_call(
    service: DemoService, override: dict[str, Any], fragment: str
) -> None:
    outcome = service.validate_strategy(_payload(service, "roa-equal-monthly", **override))
    assert outcome.ok is False and outcome.strategy is None and outcome.fingerprint is None
    assert any(fragment in message for message in outcome.errors), outcome.errors
    assert service.list_runs() == []


def test_valid_strategy_has_stable_canonical_json_and_economic_fingerprint(
    service: DemoService,
) -> None:
    payload = _payload(service, "roa-equal-monthly")
    first = service.validate_strategy(payload)
    shuffled = {
        **payload,
        "tickers": [*reversed(payload["tickers"]), "syal", " SYAL "],
        "name": "A different display name",
        "description": "Display text is not economic identity.",
    }
    second = service.validate_strategy(shuffled)
    assert first.ok and second.ok and first.strategy and second.strategy
    assert first.fingerprint == second.fingerprint  # duplicate/ordered/cased tickers collapse
    assert first.fingerprint == fingerprint_request(first.strategy.to_backtest_request())
    assert first.canonical_json != second.canonical_json  # names differ
    assert first.canonical_json == service.validate_strategy(payload).canonical_json
    assert json.loads(first.canonical_json or "")["tickers"] == sorted(payload["tickers"])
    assert first.ranking_direction == "higher_is_better"
    changed = service.validate_strategy({**payload, "top_n": 2})
    assert changed.fingerprint != first.fingerprint


def test_unknown_tickers_and_symbols_warn_but_do_not_silently_pass(service: DemoService) -> None:
    outcome = service.validate_strategy(
        _payload(service, "roa-equal-monthly", tickers=["SYAL", "SYBR", "SYCD", "ZZZZ"])
    )
    assert outcome.ok
    assert any("ZZZZ" in w for w in outcome.warnings)
    wide = service.validate_strategy(_payload(service, "roa-equal-monthly", end_date="2030-01-01"))
    assert any("demo prices cover" in w for w in wide.warnings)


def test_presets_cover_three_successes_and_four_expected_unavailable() -> None:
    presets = build_presets()
    assert [p.key for p in presets if p.expectation == "success"] == list(SUCCESS_KEYS)
    assert {p.key for p in presets if p.expectation == "unavailable"} == set(UNAVAILABLE)
    assert {p.payload["baseline"] for p in presets[:3]} == {
        "equal_weight",
        "inverse_vol",
        "min_variance",
    }


# ----------------------------------------------------------------- backtests
@pytest.mark.parametrize("key", SUCCESS_KEYS)
def test_preset_runs_through_the_real_engine_and_persists(service: DemoService, key: str) -> None:
    result = _run(service, key)
    assert result.status is PortfolioRunStatus.SUCCESS and result.failure_reason is None
    reloaded = service.load_run(result.run_id)
    assert reloaded == result  # what is shown is exactly what is persisted
    assert reloaded is not None and reloaded.metrics is not None
    assert (
        fingerprint_request(reloaded.request)
        == service.validate_strategy(_payload(service, key)).fingerprint
    )
    successful = [r for r in reloaded.rebalances if r.status is RebalanceStatus.SUCCESS]
    assert len(successful) == reloaded.metrics.successful_rebalance_count >= 5
    assert reloaded.metrics.transaction_cost_total == pytest.approx(
        sum(r.cost_amount for r in successful)
    )
    for rebalance in successful:
        assert rebalance.decision_at < rebalance.target_effective_at
        targets = [t.target_weight_after for t in rebalance.transitions]
        assert sum(targets) == pytest.approx(1.0, abs=1e-9)
        assert sum(1 for w in targets if w > 0) == reloaded.request.top_n
        gross = sum(abs(t.delta_weight) for t in rebalance.transitions)
        assert gross == pytest.approx(rebalance.gross_turnover)
        assert sum(t.allocated_cost for t in rebalance.transitions) == pytest.approx(
            rebalance.cost_amount
        )
    assert reloaded.equity[0].nav == reloaded.request.initial_nav
    assert reloaded.final_nav == pytest.approx(reloaded.equity[-1].nav)
    assert all(point.benchmark_nav is not None for point in reloaded.equity)
    assert reloaded.audit is not None and reloaded.audit.passed
    assert "provider_adjusted_history_not_vintage_pit" in reloaded.warnings
    rows = equity_rows(reloaded)
    assert len(rows) == len(reloaded.equity) and allocation_rows(reloaded)
    cells = {cell.key: cell for cell in metric_cells(reloaded)}
    assert cells["final_nav"].value == reloaded.final_nav
    assert cells["max_drawdown"].value == reloaded.metrics.max_drawdown


def test_every_decision_used_only_evidence_public_at_its_decision_time(
    service: DemoService,
) -> None:
    result = _run(service, "roa-equal-monthly")
    seen_years: set[str] = set()
    for rebalance in result.rebalances:
        detail = decision_detail(rebalance)
        for filing in service.filings_by_accession(detail.accessions):
            assert filing.available_at <= rebalance.decision_at, filing.accession
        seen_years.update(str(row["Report period"]) for row in detail.rows)
        # A decision uses a fiscal year only after its 10-K could have been read.
        for row in detail.rows:
            if row["Report period"] == "FY2023":
                assert rebalance.decision_at > decision_time(date(2024, 2, 8))
    assert {"FY2022", "FY2023"} <= seen_years


def test_drifted_weights_differ_from_targets_and_effective_follows_decision(
    service: DemoService,
) -> None:
    result = _run(service, "roa-equal-monthly")
    later = [r for r in result.rebalances if r.status is RebalanceStatus.SUCCESS][1:]
    drifted = [
        t for r in later for t in r.transitions if t.current_weight_before > 0 and t.delta_weight
    ]
    assert drifted  # weights drifted with returns between rebalances
    assert all(t.target_weight_after != t.current_weight_before for t in drifted)
    assert all(r.target_effective_at.date() > r.decision_at.date() for r in result.rebalances)


@pytest.mark.parametrize(("key", "reason"), sorted(UNAVAILABLE.items()))
def test_unavailable_configurations_fail_truthfully(
    service: DemoService, key: str, reason: str
) -> None:
    result = _run(service, key)
    assert result.status is PortfolioRunStatus.UNAVAILABLE
    assert result.failure_reason == reason
    assert result.final_nav is None and not result.equity and not result.rebalances
    assert all(cell.value is None for cell in metric_cells(result))
    assert all(cell.unavailable_reason for cell in metric_cells(result))
    html = render_research_report(result)
    assert "Research status: UNAVAILABLE" in html and "Research status: SUCCESS" not in html
    probe = service.probe_availability(result.request)
    assert probe.rows or probe.note
    if key == "diag-share-classes":
        assert all(ok for _t, ok, _why in probe.rows)  # factors fine; identity is the problem
    if key == "diag-valuation-no-market-cap":
        assert all(not ok for _t, ok, _why in probe.rows)
        assert any("Market capitalization unavailable" in why for _t, _ok, why in probe.rows)
    if key == "diag-evidence-not-public":
        assert all("Missing current or prior revenue" in why for _t, _ok, why in probe.rows)


def test_a_repeated_token_returns_the_saved_run_instead_of_running_twice(
    service: DemoService,
) -> None:
    first = _run(service, "roa-equal-monthly", token="same-click")
    again = _run(service, "roa-equal-monthly", token="same-click")
    assert again.run_id == first.run_id
    assert len(service.list_runs()) == 1
    other = _run(service, "roa-equal-monthly", token="deliberate-rerun")
    assert other.run_id != first.run_id and len(service.list_runs()) == 2
    # Same economics, different run identity: fingerprint equal, run IDs and files differ.
    assert fingerprint_request(other.request) == fingerprint_request(first.request)
    assert other.final_nav == pytest.approx(first.final_nav or 0.0, abs=1e-12)


def test_saved_runs_survive_a_restart_without_rerunning_research(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _run(DemoService(demo_dir), "roa-equal-monthly").run_id
    calls: list[object] = []
    from equitytrace.portfolio.engine import BacktestEngine

    original = BacktestEngine.run
    monkeypatch.setattr(
        BacktestEngine, "run", lambda self, *a, **k: calls.append(1) or original(self, *a, **k)
    )
    restarted = DemoService(demo_dir)  # a fresh object, as after restarting the app
    assert [r.run_id for r in restarted.list_runs()] == [run_id]
    loaded = restarted.load_run(run_id)
    assert loaded is not None and loaded.status is PortfolioRunStatus.SUCCESS
    DemoService.report_html(loaded)
    DemoService.request_json(loaded)
    DemoService.result_json(loaded)
    assert calls == []


@pytest.mark.parametrize(
    "run_id",
    ["", "x", "../../etc/passwd", "'; DROP TABLE portfolio_runs;--", "0" * 31, "G" * 32, "0" * 33],
)
def test_run_ids_are_validated_before_touching_the_database(
    service: DemoService, run_id: str
) -> None:
    assert service.load_run(run_id) is None
    assert service.load_run("0" * 32) is None


# -------------------------------------------------------------------- exports
def test_exports_regenerate_identically_and_match_the_cli_export(
    service: DemoService, tmp_path: Path
) -> None:
    result = _run(service, "roa-equal-monthly")
    loaded = service.load_run(result.run_id)
    assert loaded is not None
    report = DemoService.report_html(loaded)
    assert report == DemoService.report_html(loaded)
    assert DemoService.request_json(loaded) == DemoService.request_json(loaded)
    assert DemoService.result_json(loaded) == DemoService.result_json(loaded)
    request_path, result_path = export_run_json(loaded, tmp_path / "cli-export")
    assert request_path.read_bytes() == DemoService.request_json(loaded).encode("utf-8")
    assert result_path.read_bytes() == DemoService.result_json(loaded).encode("utf-8")
    assert "SYNTHETIC DEMO ONLY" in report and "Research status: SUCCESS" in report
    assert "hypothetical" in report.lower()
    assert "provider_adjusted_history_not_vintage_pit" in report
    banner_start = report.index('<p role="note"')
    closing = "</p>" + chr(10)
    banner_end = report.index(closing, banner_start) + len(closing)
    assert report[:banner_start] + report[banner_end:] == render_research_report(loaded)


def test_download_names_are_predictable_and_path_safe(service: DemoService) -> None:
    result = _run(service, "roa-equal-monthly")
    name = DemoService.download_name(result, "report.html")
    assert name == f"equitytrace-demo-{result.run_id[:16]}-report.html"
    hostile = result.model_copy(update={"run_id": "../../evil name" + chr(92) + "x"})
    safe = DemoService.download_name(hostile, "report.html")
    assert re.fullmatch(r"equitytrace-demo-[A-Za-z0-9._-]+-report\.html", safe)
