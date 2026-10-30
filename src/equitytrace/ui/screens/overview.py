"""Screen A — research overview and the explicit "load demo" action."""

from __future__ import annotations

import streamlit as st

from equitytrace.demo.prepare import DemoPreparationError
from equitytrace.demo.service import DemoService
from equitytrace.factors.registry import list_factors
from equitytrace.ui.components import goto_button, limitations_list, synthetic_banner
from equitytrace.ui.state import clear_dataset_state

LAYERS = (
    ("1 · Evidence", "Synthetic SEC-style 10-K facts with filing and public-availability times"),
    ("2 · Statements", "Canonical income, balance-sheet and cash-flow metrics with provenance"),
    ("3 · Factors", "Transparent formulas such as ROA and revenue growth"),
    ("4 · Rankings", "Deterministic ranks using each factor's registered direction"),
    ("5 · Portfolio engine", "Decision vs effective dates, drifting weights, explicit costs"),
    ("6 · Reports", "Standalone HTML and deterministic JSON from the saved run"),
)


def render(service: DemoService) -> None:
    st.title("EquityTrace — Transparent Equity Research")
    st.caption(
        "A point-in-time research platform. This is a research tool, not a trading application."
    )
    synthetic_banner()

    status = service.status()
    left, right = st.columns([3, 2])
    with left:
        st.subheader("What this demo shows")
        st.markdown(
            "EquityTrace only lets a research decision use evidence that was **public at the "
            "decision time**. A fiscal year ending on 31 December is not usable until its 10-K "
            "is accepted, weeks later. This demo runs the real engine on a small, fully "
            "fictional dataset so you can watch that rule at work, offline."
        )
        columns = st.columns(len(LAYERS))
        for column, (title, caption) in zip(columns, LAYERS, strict=True):
            with column:
                st.markdown(f"**{title}**")
                st.caption(caption)
    with right:
        st.subheader("Dataset")
        if status.prepared:
            st.success("Synthetic demo is loaded.", icon="✅")
            st.write(
                f"**{len(status.tickers)}** fictional securities · "
                f"**{status.fact_count}** financial facts · **{status.bar_count:,}** daily bars"
            )
            st.write(
                f"Prices {status.price_start.isoformat()} → {status.price_end.isoformat()} · "
                f"{status.saved_run_count} saved run(s)"
            )
            st.caption(f"Tickers: {', '.join(status.tickers)} · benchmark SYNX (fictional)")
            st.caption(f"Content digest: {(status.content_digest or '')[:16]}…")
        else:
            st.error(status.message, icon="🧱")
            if st.button("Load synthetic demo", type="primary", key="load_demo"):
                _prepare(service, overwrite=False)
        st.write(f"**Fundamental factors in the registry:** {', '.join(list_factors())}")
        st.caption(
            "Valuation factors need share counts and raw prices, which this dataset does not "
            "store; they report Unavailable instead of a guess."
        )

    if status.prepared:
        st.subheader("Quick demonstration")
        steps = (
            ("1. Explore evidence", "Data explorer", "Pick a company and an as-of date."),
            ("2. Build a strategy", "Strategy builder", "Choose a preset, tweak, validate."),
            ("3. Run it", "Strategy builder", "Nothing runs until you press Run."),
            ("4. Inspect results", "Results", "NAV, drawdown, benchmark, weights."),
            ("5. Audit decisions", "Inspect decisions", "Why each name was chosen."),
            ("6. Export", "Reports & exports", "HTML report and JSON records."),
        )
        for column, (title, screen, caption) in zip(st.columns(len(steps)), steps, strict=True):
            with column:
                st.markdown(f"**{title}**")
                st.caption(caption)
                goto_button("Open", screen, key=f"overview_open_{title}")
        with st.expander("Rebuild the demo database"):
            st.write(
                "Rebuilding recreates the synthetic dataset and **deletes saved demo runs**. "
                "Only a database that carries the EquityTrace demo marker can be replaced."
            )
            confirmed = st.checkbox(
                "I understand saved demo runs will be deleted", key="rebuild_ok"
            )
            if st.button("Rebuild demo", disabled=not confirmed, key="rebuild_demo"):
                _prepare(service, overwrite=True)

    st.subheader("Limits you should keep in mind")
    limitations_list()


def _prepare(service: DemoService, *, overwrite: bool) -> None:
    try:
        with st.spinner("Building the synthetic demo database…"):
            outcome = service.prepare(overwrite=overwrite)
    except DemoPreparationError as exc:
        st.error(f"The demo could not be prepared: {exc}")
        return
    except Exception as exc:
        st.error(f"Unexpected error while preparing the demo: {type(exc).__name__}: {exc}")
        return
    clear_dataset_state()
    st.session_state["prepare_message"] = f"Demo {outcome.action}."
    st.rerun()
