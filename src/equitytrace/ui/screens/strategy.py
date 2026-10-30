"""Screen C — strategy builder. Validation is pure; only the Run button executes research."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any

import streamlit as st

from equitytrace.demo.explain import explain_reason
from equitytrace.demo.service import DemoService, ValidationOutcome
from equitytrace.factors.registry import get_factor, list_factors
from equitytrace.portfolio.models import BacktestResult, PortfolioBaseline, PortfolioRunStatus
from equitytrace.ui.components import (
    fmt_num,
    goto_button,
    status_label,
    synthetic_banner,
    table,
)
from equitytrace.ui.state import load_run_cached

SCHEDULES = ("monthly", "quarterly", "explicit")
BENCHMARK_OPTIONS = ("SYNX", "(none)")


def _apply_payload(payload: dict[str, Any]) -> None:
    """Write a strategy payload into the form widgets (runs as a button callback)."""
    st.session_state.update(
        {
            "sb_name": str(payload.get("name", "")),
            "sb_desc": str(payload.get("description") or ""),
            "sb_tickers": [str(t) for t in payload.get("tickers", [])],
            "sb_factor": str(payload.get("factor", "roa")),
            "sb_top_n": int(payload.get("top_n", 3)),
            "sb_baseline": str(payload.get("baseline", "equal_weight")),
            "sb_schedule": str(payload.get("schedule", "monthly")),
            "sb_start": date.fromisoformat(str(payload.get("start_date", "2023-07-03"))),
            "sb_end": date.fromisoformat(str(payload.get("end_date", "2024-12-31"))),
            "sb_explicit": "\n".join(str(d) for d in payload.get("explicit_dates", [])),
            "sb_cost": float(payload.get("cost_bps", 10.0)),
            "sb_nav": float(payload.get("initial_nav", 1.0)),
            "sb_benchmark": str(payload.get("benchmark_symbol") or "(none)"),
            "sb_calendar": str(payload.get("calendar_symbol", "SYNX")),
            "sb_view": "validate",
        }
    )


def _load_preset(service: DemoService) -> None:
    key = st.session_state.get("sb_preset")
    for preset in service.presets():
        if preset.key == key:
            _apply_payload(preset.payload)
            st.session_state["sb_import_error"] = None


def _import_json(service: DemoService) -> None:
    text = str(st.session_state.get("sb_json_text", "")).strip()
    st.session_state["sb_import_error"] = None
    if not text:
        st.session_state["sb_import_error"] = "Paste strategy JSON first."
        return
    try:
        payload = json.loads(text)
    except ValueError as exc:
        st.session_state["sb_import_error"] = f"Not valid JSON: {exc}"
        return
    if not isinstance(payload, dict):
        st.session_state["sb_import_error"] = "Strategy JSON must be an object."
        return
    outcome = service.validate_strategy(payload)
    if not outcome.ok:
        st.session_state["sb_import_error"] = "Invalid strategy: " + "; ".join(outcome.errors)
        return
    _apply_payload(json.loads(outcome.canonical_json or "{}"))


def _set_view(view: str) -> None:
    st.session_state["sb_view"] = view


def _bump_epoch() -> None:
    st.session_state["run_epoch"] = int(st.session_state.get("run_epoch", 0)) + 1


def _payload_from_form() -> dict[str, Any]:
    state = st.session_state
    explicit = [
        line.strip() for line in str(state.get("sb_explicit", "")).splitlines() if line.strip()
    ]
    benchmark = state.get("sb_benchmark")
    return {
        "name": state.get("sb_name", ""),
        "description": state.get("sb_desc", ""),
        "tickers": list(state.get("sb_tickers", [])),
        "factor": state.get("sb_factor", "roa"),
        "top_n": state.get("sb_top_n", 3),
        "baseline": state.get("sb_baseline", "equal_weight"),
        "start_date": state.get("sb_start"),
        "end_date": state.get("sb_end"),
        "schedule": state.get("sb_schedule", "monthly"),
        "explicit_dates": explicit if state.get("sb_schedule") == "explicit" else [],
        "calendar_symbol": state.get("sb_calendar", "SYNX"),
        "benchmark_symbol": None if benchmark in (None, "(none)") else benchmark,
        "cost_bps": state.get("sb_cost", 10.0),
        "initial_nav": state.get("sb_nav", 1.0),
        "provider": "twelve_data",
        "adjustment_mode": "all",
    }


def _run_token(outcome: ValidationOutcome) -> str:
    """Stable per (session, exact strategy, epoch): a double click reuses it."""
    session_id = st.session_state.setdefault("session_nonce", uuid.uuid4().hex)
    epoch = int(st.session_state.get("run_epoch", 0))
    raw = f"{session_id}|{epoch}|{outcome.canonical_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render(service: DemoService) -> None:
    st.header("Strategy builder")
    synthetic_banner()
    status = service.status()
    if not status.prepared:
        st.info("Load the synthetic demo on the *Overview* screen first.")
        goto_button("Go to Overview", "Overview", key="sb_goto_overview", primary=True)
        return

    presets = service.presets()
    if "sb_name" not in st.session_state:
        _apply_payload(presets[0].payload)
    st.session_state.setdefault("sb_import_error", None)

    st.subheader("1 · Start from a preset")
    by_key = {p.key: p for p in presets}
    st.selectbox(
        "Preset",
        list(by_key),
        format_func=lambda k: (
            f"{'✅' if by_key[k].expectation == 'success' else '⚠️'} {by_key[k].title}"
        ),
        key="sb_preset",
    )
    chosen = by_key[st.session_state.get("sb_preset", presets[0].key)]
    st.caption(chosen.summary)
    st.button("Load preset into the form", on_click=_load_preset, args=(service,), key="sb_load")

    st.subheader("2 · Configure")
    left, right = st.columns(2)
    with left:
        st.text_input("Strategy name", key="sb_name", max_chars=120)
        st.text_area("Description", key="sb_desc", max_chars=500, height=80)
        st.multiselect(
            "Universe (tickers; type to add one that is not listed)",
            options=list(status.tickers),
            key="sb_tickers",
            accept_new_options=True,
            help="SYGA and SYGB are two classes of one issuer. Selecting both is refused.",
        )
        st.selectbox("Factor", list_factors(), key="sb_factor")
        try:
            direction = get_factor(str(st.session_state.get("sb_factor"))).ranking_direction
            st.caption(f"Ranking direction (from the registry): **{direction}**")
        except Exception:  # unknown factor cannot happen via the selectbox
            st.caption("Ranking direction unavailable.")
        st.number_input("Top-N names to hold", min_value=0, max_value=20, step=1, key="sb_top_n")
        st.selectbox(
            "Portfolio baseline",
            [item.value for item in PortfolioBaseline],
            key="sb_baseline",
            help="Inverse-volatility and minimum-variance need 253 aligned closes; there is "
            "no silent fallback to another method.",
        )
    with right:
        dates = st.columns(2)
        with dates[0]:
            st.date_input(
                "Start date",
                min_value=date(2000, 1, 1),
                max_value=date(2035, 12, 31),
                key="sb_start",
            )
        with dates[1]:
            st.date_input(
                "End date", min_value=date(2000, 1, 1), max_value=date(2035, 12, 31), key="sb_end"
            )
        st.selectbox("Rebalance schedule", SCHEDULES, key="sb_schedule")
        if st.session_state.get("sb_schedule") == "explicit":
            st.text_area(
                "Explicit decision dates (YYYY-MM-DD, one per line)", key="sb_explicit", height=90
            )
        st.number_input(
            "Transaction cost (basis points of turnover)", min_value=0.0, step=1.0, key="sb_cost"
        )
        st.number_input("Initial NAV", min_value=0.0, step=0.5, key="sb_nav", format="%.4f")
        st.selectbox("Benchmark", BENCHMARK_OPTIONS, key="sb_benchmark")
        st.selectbox("Calendar symbol", ["SYNX"], key="sb_calendar")

    with st.expander("Advanced: load a strategy from JSON"):
        st.text_area("Strategy JSON", key="sb_json_text", height=140)
        st.button(
            "Load JSON into the form", on_click=_import_json, args=(service,), key="sb_import"
        )
        if st.session_state.get("sb_import_error"):
            st.error(st.session_state["sb_import_error"])

    payload = _payload_from_form()
    outcome = service.validate_strategy(payload)

    st.subheader("3 · Validate and inspect")
    st.caption("Validation is a pure check of the form. Editing a field never starts a backtest.")
    cols = st.columns(3)
    with cols[0]:
        st.button(
            "Validate strategy", on_click=_set_view, args=("validate",), key="sb_btn_validate"
        )
    with cols[1]:
        st.button("Preview canonical JSON", on_click=_set_view, args=("json",), key="sb_btn_json")
    with cols[2]:
        st.button(
            "Show economic fingerprint", on_click=_set_view, args=("fingerprint",), key="sb_btn_fp"
        )
    view = st.session_state.get("sb_view", "validate")
    if not outcome.ok:
        st.error("The strategy is not valid yet:")
        for message in outcome.errors:
            st.markdown(f"- `{message}`")
    else:
        for warning in outcome.warnings:
            st.warning(warning)
        if view == "validate":
            st.success(
                f"Valid strategy. Factor ranks **{outcome.ranking_direction}**. "
                "It has not been run.",
                icon="✅",
            )
        elif view == "json":
            st.code(outcome.canonical_json, language="json")
        else:
            st.code(outcome.fingerprint or "")
            st.caption(
                "SHA-256 of the canonical economic request: universe, factor, top-N, baseline, "
                "dates, schedule, costs, NAV, benchmark and provider. Name, description, run "
                "ID and timestamps are excluded, so the same economics always match."
            )

    st.subheader("4 · Run")
    action = st.columns([1, 1, 1])
    with action[0]:
        run_clicked = st.button(
            "Run backtest", type="primary", disabled=not outcome.ok, key="sb_run"
        )
    with action[1]:
        st.download_button(
            "Download strategy JSON",
            data=outcome.canonical_json or "",
            file_name="equitytrace-demo-strategy.json",
            mime="application/json",
            disabled=not outcome.ok,
            key="sb_download",
        )
    with action[2]:
        st.button("Run again (new run)", on_click=_bump_epoch, key="sb_again")
    st.caption(
        "A repeated click on the same unchanged strategy opens the saved run instead of "
        "creating a duplicate. Use *Run again* to deliberately create a second run."
    )

    if run_clicked and outcome.ok and outcome.strategy is not None:
        try:
            with st.spinner("Running the real backtest engine…"):
                result = service.run_strategy(outcome.strategy, token=_run_token(outcome))
        except Exception as exc:
            st.error(f"The run could not be completed: {type(exc).__name__}: {exc}")
        else:
            st.session_state["selected_run_id"] = result.run_id
            st.session_state.setdefault("run_cache", {})[result.run_id] = result
            st.session_state["last_run_id"] = result.run_id

    last = st.session_state.get("last_run_id")
    if last:
        shown = load_run_cached(service, last)
        if shown is not None:
            _show_outcome(service, shown)


def _show_outcome(service: DemoService, result: BacktestResult) -> None:
    st.subheader("Last run")
    st.caption(f"Run ID `{result.run_id}` · {status_label(result.status.value)}")
    if result.status is PortfolioRunStatus.SUCCESS:
        st.success(
            f"Research status SUCCESS · final NAV {fmt_num(result.final_nav)} (hypothetical, "
            "synthetic).",
            icon="✅",
        )
    elif result.status is PortfolioRunStatus.UNAVAILABLE:
        st.warning(
            f"Research status UNAVAILABLE — `{result.failure_reason}`. No performance exists "
            "for this configuration.",
            icon="⚠️",
        )
        st.write(explain_reason(result.failure_reason))
        probe = service.probe_availability(result.request)
        if probe.note:
            st.caption(probe.note)
        if probe.rows:
            st.caption(f"Factor availability at the first decision ({probe.probe_time}):")
            table(
                [
                    {"Ticker": t, "Usable": "yes" if ok else "NO", "Detail": why}
                    for t, ok, why in probe.rows
                ]
            )
    else:
        st.error(
            f"Research status {result.status.value.upper()} — `{result.failure_reason}`. "
            "No performance is shown.",
            icon="❌",
        )
        st.write(explain_reason(result.failure_reason))
    cols = st.columns(2)
    with cols[0]:
        goto_button("Open results", "Results", key="sb_open_results", primary=True)
    with cols[1]:
        goto_button("Inspect decisions", "Inspect decisions", key="sb_open_inspect")
