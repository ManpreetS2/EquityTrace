"""Streamlit demo UI behaviour, exercised with Streamlit's own AppTest harness."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from equitytrace.demo.prepare import DemoPaths
from equitytrace.demo.service import DemoService
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.ui.components import SCREENS

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "src" / "equitytrace" / "ui" / "app.py")
TIMEOUT = 180


@pytest.fixture
def engine_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every real backtest the UI triggers."""
    calls: list[str] = []
    original: Callable[..., object] = BacktestEngine.run

    def counting(self: BacktestEngine, request: object, *args: object, **kwargs: object) -> object:
        calls.append("run")
        return original(self, request, *args, **kwargs)

    monkeypatch.setattr(BacktestEngine, "run", counting)
    return calls


def _app(directory: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv("EQUITYTRACE_DEMO_DIR", str(directory))
    return AppTest.from_file(APP, default_timeout=TIMEOUT).run()


def _go(app: AppTest, screen: str) -> AppTest:
    app.session_state["screen"] = screen
    return app.run()


def _texts(elements: object) -> str:
    return "\n".join(str(getattr(item, "value", "")) for item in elements)  # type: ignore[attr-defined]


def _ok(app: AppTest) -> None:
    assert not app.exception, [e.value for e in app.exception]


# ------------------------------------------------------------------- startup
def test_app_starts_without_preparing_anything_and_every_screen_handles_a_missing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    directory = tmp_path / "never-created"
    app = _app(directory, monkeypatch)
    _ok(app)
    assert "EquityTrace" in app.title[0].value
    assert "SYNTHETIC DEMO DATA" in _texts(app.warning)
    assert [b.label for b in app.button] == ["Load synthetic demo"]
    for screen in SCREENS:
        app = _go(app, screen)
        _ok(app)
        assert app.session_state["screen"] == screen
    for _ in range(3):
        app.run()
    assert not directory.exists()  # reruns never prepare data
    assert engine_calls == []


def test_load_button_prepares_exactly_once_and_reruns_do_not_touch_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "demo"
    app = _app(directory, monkeypatch)
    app.button(key="load_demo").click().run()
    _ok(app)
    assert "Synthetic demo is loaded." in _texts(app.success)
    database = DemoPaths(directory).database
    stamp = database.stat().st_mtime_ns
    for screen in ("Overview", "Data explorer", "Strategy builder", "Overview"):
        app = _go(app, screen)
        _ok(app)
    assert database.stat().st_mtime_ns == stamp
    assert "**8** fictional securities" in _texts(app.markdown)


def test_rebuild_requires_confirmation_and_clears_saved_runs(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(demo_dir, monkeypatch)
    app = _go(app, "Strategy builder")
    app.button(key="sb_run").click().run()
    assert DemoService(demo_dir).status().saved_run_count == 1
    app = _go(app, "Overview")
    assert app.button(key="rebuild_demo").disabled is True
    app.checkbox(key="rebuild_ok").check().run()
    assert app.button(key="rebuild_demo").disabled is False
    app.button(key="rebuild_demo").click().run()
    _ok(app)
    assert DemoService(demo_dir).status().saved_run_count == 0


# ------------------------------------------------------------ explicit actions
def test_widget_changes_and_reruns_never_start_a_backtest(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    _ok(app)
    app.number_input(key="sb_top_n").set_value(2).run()
    app.selectbox(key="sb_factor").select("revenue_growth").run()
    app.selectbox(key="sb_baseline").select("inverse_vol").run()
    app.selectbox(key="sb_schedule").select("quarterly").run()
    app.text_input(key="sb_name").set_value("Edited name").run()
    app.button(key="sb_btn_json").click().run()
    app.button(key="sb_btn_fp").click().run()
    app.button(key="sb_btn_validate").click().run()
    for _ in range(3):
        app.run()
    _ok(app)
    assert engine_calls == []
    assert DemoService(demo_dir).list_runs() == []
    assert "It has not been run." in _texts(app.success)


def test_run_requires_a_click_and_a_double_click_creates_one_run(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.button(key="sb_run").click().run()
    _ok(app)
    assert engine_calls == ["run"]
    run_id = str(app.session_state["selected_run_id"])
    app.button(key="sb_run").click().run()  # second click on the unchanged strategy
    assert engine_calls == ["run"]
    assert app.session_state["selected_run_id"] == run_id
    assert len(DemoService(demo_dir).list_runs()) == 1
    assert "Research status SUCCESS" in _texts(app.success)
    app.button(key="sb_again").click().run()
    app.button(key="sb_run").click().run()  # deliberate new run
    assert engine_calls == ["run", "run"]
    assert len(DemoService(demo_dir).list_runs()) == 2


def test_inspecting_results_and_exports_never_reruns_research(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.button(key="sb_run").click().run()
    assert engine_calls == ["run"]
    runs_before = DemoService(demo_dir).list_runs()
    for screen in ("Results", "Inspect decisions", "Reports & exports", "Results"):
        app = _go(app, screen)
        _ok(app)
        app.run()
    assert engine_calls == ["run"]
    assert DemoService(demo_dir).list_runs() == runs_before
    results = _go(app, "Results")
    labels = {m.label: m.value for m in results.metric}
    assert labels["Final NAV"] != "Unavailable" and labels["Successful rebalances"] == "17"
    assert "SUCCESS" in _texts(results.success)
    assert len(results.get("vega_lite_chart")) == 3  # NAV vs benchmark, drawdown, allocation
    assert "does not start at zero" in _texts(results.caption)
    assert "Metric definitions and units" in [e.label for e in results.expander]
    exports = _go(results, "Reports & exports")
    assert len(exports.get("download_button")) == 4
    inspected = _go(exports, "Inspect decisions")
    assert [m.label for m in inspected.metric][:2] == ["Status", "Gross turnover"]
    assert "different events" in _texts(inspected.info)


def test_selecting_a_rebalance_shows_the_decision_without_running_anything(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.button(key="sb_run").click().run()
    app = _go(app, "Inspect decisions")
    app.selectbox(key="insp_idx").select(5).run()
    _ok(app)
    assert engine_calls == ["run"]
    assert len(app.dataframe) >= 2  # selected names + evidence (+ filings)


# --------------------------------------------------------- form and validation
def test_preset_loading_fills_the_form_and_invalid_input_blocks_the_run(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.selectbox(key="sb_preset").select("margin-min-variance-monthly").run()
    app.button(key="sb_load").click().run()
    assert app.session_state["sb_factor"] == "operating_margin_change"
    assert app.session_state["sb_baseline"] == "min_variance"
    assert app.session_state["sb_top_n"] == 3

    app.number_input(key="sb_top_n").set_value(0).run()
    assert app.button(key="sb_run").disabled is True
    assert "top_n" in _texts(app.markdown)
    app.number_input(key="sb_top_n").set_value(9).run()
    assert "top_n must be <= number of unique universe tickers" in _texts(app.markdown)
    app.number_input(key="sb_top_n").set_value(3).run()
    app.date_input(key="sb_start").set_value(date(2025, 1, 1)).run()
    assert app.button(key="sb_run").disabled is True
    assert "start_date must be on or before end_date" in _texts(app.markdown)
    app.multiselect(key="sb_tickers").set_value([]).run()
    assert app.button(key="sb_run").disabled is True
    assert engine_calls == []


def test_malformed_and_invalid_json_import_is_reported_not_crashed(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    for text, expected in (
        ("", "Paste strategy JSON first."),
        ("{not json", "Not valid JSON"),
        ("[1, 2]", "must be an object"),
        ('{"name": "x"}', "Invalid strategy"),
    ):
        app.text_area(key="sb_json_text").set_value(text).run()
        app.button(key="sb_import").click().run()
        _ok(app)
        assert expected in _texts(app.error), text
    good = ROOT / "examples" / "demo" / "strategies" / "growth-inverse-vol-quarterly.json"
    app.text_area(key="sb_json_text").set_value(good.read_text(encoding="utf-8")).run()
    app.button(key="sb_import").click().run()
    assert app.session_state["sb_factor"] == "revenue_growth"
    assert app.session_state["sb_baseline"] == "inverse_vol"


# ---------------------------------------------------------- truthful failures
@pytest.mark.parametrize(
    ("preset", "reason"),
    [
        ("diag-share-classes", "multiple_securities_same_issuer_unsupported"),
        ("diag-valuation-no-market-cap", "factor_universe_too_small"),
        ("diag-short-price-history", "baseline_unavailable"),
        ("diag-evidence-not-public", "factor_universe_too_small"),
    ],
)
def test_unavailable_runs_are_explained_and_never_shown_as_performance(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, preset: str, reason: str
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.selectbox(key="sb_preset").select(preset).run()
    app.button(key="sb_load").click().run()
    app.button(key="sb_run").click().run()
    _ok(app)
    warnings = _texts(app.warning)
    assert "Research status UNAVAILABLE" in warnings and reason in warnings
    assert "Research status SUCCESS" not in _texts(app.success)
    assert "Factor availability at the first decision" in _texts(app.caption) or (
        "Calendar" in _texts(app.caption)
    )

    results = _go(app, "Results")
    _ok(results)
    assert "UNAVAILABLE" in _texts(results.warning)
    assert not results.metric  # no NAV, return or drawdown cards
    assert "never display NAV" in _texts(results.caption)
    inspect = _go(results, "Inspect decisions")
    _ok(inspect)
    assert "no rebalance records" in _texts(inspect.warning)


def test_unknown_ticker_is_an_unavailable_run_not_a_crash(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.multiselect(key="sb_tickers").set_value(["SYAL", "SYBR", "SYCD", "ZZZZ", "<b>x</b>"]).run()
    assert "ZZZZ is not in the demo dataset" in _texts(app.warning)
    app.button(key="sb_run").click().run()
    _ok(app)
    assert "issuer_identity_unavailable" in _texts(app.warning)


# ------------------------------------------------------------ run lookup input
def test_run_lookup_handles_missing_and_hostile_ids(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Results")
    assert "No saved runs yet" in _texts(app.info)
    app = _go(app, "Strategy builder")
    app.button(key="sb_run").click().run()
    app = _go(app, "Results")
    for typed, expected in (
        ("../../etc/passwd", "exactly 32 lowercase hexadecimal"),
        ("'; drop table portfolio_runs;--", "exactly 32 lowercase hexadecimal"),
        ("0" * 32, "No saved run has that ID"),
    ):
        app.text_input(key="results_run_id_input").set_value(typed).run()
        _ok(app)
        assert expected in _texts(app.error), typed
    assert DemoService(demo_dir).status().saved_run_count == 1


def test_ui_code_cannot_inject_html_or_hold_database_connections() -> None:
    """Static guard: no raw-HTML rendering, no cached/global DuckDB handles in the UI layer."""
    sources = {
        p: p.read_text(encoding="utf-8") for p in (ROOT / "src/equitytrace/ui").rglob("*.py")
    }
    assert sources
    for path, text in sources.items():
        assert "unsafe_allow_html" not in text, path
        assert not re.search(r"^\s*(import duckdb|from duckdb)", text, re.M), path
        assert "cache_resource" not in text and "@st.cache" not in text, path
        assert "components.html" not in text, path
        assert "subprocess" not in text and "requests" not in text and "httpx" not in text, path


# ------------------------------------------------------------ explorer screen
def test_explorer_follows_the_as_of_date_and_survives_stale_selections(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Data explorer")
    _ok(app)
    app.date_input(key="ex_asof").set_value(date(2022, 7, 1)).run()
    _ok(app)
    assert app.selectbox(key="ex_period").options == ["FY2021"]  # only FY2021 was public yet
    app.date_input(key="ex_asof").set_value(date(2024, 3, 20)).run()
    assert app.selectbox(key="ex_period").options[0] == "FY2023"
    # Changing the date or issuer after choosing a fiscal year must not leave a stale selection.
    app.selectbox(key="ex_period").select(2023).run()
    app.date_input(key="ex_asof").set_value(date(2023, 3, 1)).run()
    app.selectbox(key="ex_ticker").select("SYBR").run()
    _ok(app)
    assert app.selectbox(key="ex_period").options == ["FY2021"]  # SYBR files FY2022 on 2023-03-14
    assert engine_calls == []


def test_explorer_comparison_shows_a_fact_becoming_public(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Data explorer")
    app.button(key="cmp_example").click().run()
    _ok(app)
    assert app.session_state["ex_ticker"] == "SYBR"
    tables = [df.value for df in app.dataframe]
    comparison = next(t for t in tables if "At 2024-03-13" in t.columns)
    revenue = comparison[comparison["Metric"] == "revenue"].iloc[0]
    assert revenue["At 2024-03-13"] == "Unavailable"
    assert revenue["At 2024-03-15"] == "$3,250.0M"
    assert revenue["Newly public accession"] == "0990000002-24-000001"
    filings = next(t for t in tables if "Public at this as-of?" in t.columns)
    assert list(filings["Public at this as-of?"]) == ["YES", "YES", "YES"]


# ------------------------------------------------- state across navigation / rebuild
def test_form_and_explorer_edits_survive_navigating_away_and_back(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_calls: list[str]
) -> None:
    """Regression: Streamlit drops widget state for widgets not rendered in a run."""
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.number_input(key="sb_top_n").set_value(2).run()
    app.text_input(key="sb_name").set_value("My edited strategy").run()
    app.selectbox(key="sb_factor").select("revenue_growth").run()
    for screen in ("Results", "Methodology & guide", "Data explorer"):
        app = _go(app, screen)
        if screen == "Data explorer":
            app.selectbox(key="ex_ticker").select("SYFM").run()
            app.date_input(key="ex_asof").set_value(date(2024, 5, 1)).run()
    app = _go(app, "Strategy builder")
    _ok(app)
    assert app.number_input(key="sb_top_n").value == 2
    assert app.text_input(key="sb_name").value == "My edited strategy"
    assert app.selectbox(key="sb_factor").value == "revenue_growth"
    app = _go(app, "Data explorer")
    assert app.selectbox(key="ex_ticker").value == "SYFM"
    assert app.date_input(key="ex_asof").value == date(2024, 5, 1)
    assert engine_calls == []


def test_rebuilding_the_demo_clears_every_trace_of_the_previous_runs(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _go(_app(demo_dir, monkeypatch), "Strategy builder")
    app.button(key="sb_run").click().run()
    old_run = str(app.session_state["selected_run_id"])
    assert "Last run" in _texts(app.subheader)
    app = _go(app, "Overview")
    app.checkbox(key="rebuild_ok").check().run()
    app.button(key="rebuild_demo").click().run()
    _ok(app)
    for key in ("selected_run_id", "last_run_id", "run_cache"):
        assert key not in app.session_state, key
    app = _go(app, "Strategy builder")
    _ok(app)
    assert "Last run" not in _texts(app.subheader)
    app = _go(app, "Results")
    assert "No saved runs yet" in _texts(app.info)
    assert DemoService(demo_dir).load_run(old_run) is None
