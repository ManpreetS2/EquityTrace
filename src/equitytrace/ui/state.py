"""Session helpers: one service per browser session, immutable cached runs.

No DuckDB connection is ever stored. The service opens and closes a short-lived
session per call, and saved runs are cached by id because persisted runs never
change (a different dataset or a rebuild clears the cache).
"""

from __future__ import annotations

import re

import streamlit as st

from equitytrace.demo.service import DemoService, RunSummary
from equitytrace.portfolio.models import BacktestResult
from equitytrace.ui.components import fmt_dt, status_label

_RUN_ID = re.compile(r"[0-9a-f]{32}")


def get_service() -> DemoService:
    service = st.session_state.get("service")
    if not isinstance(service, DemoService):
        service = DemoService()
        st.session_state["service"] = service
    return service


def clear_dataset_state() -> None:
    """Drop everything derived from the stored dataset (after prepare or rebuild)."""
    for key in ("run_cache", "selected_run_id", "last_run_message", "validation", "probe"):
        st.session_state.pop(key, None)


def load_run_cached(service: DemoService, run_id: str) -> BacktestResult | None:
    cache: dict[str, BacktestResult] = st.session_state.setdefault("run_cache", {})
    if run_id in cache:
        return cache[run_id]
    loaded = service.load_run(run_id)
    if loaded is not None:
        cache[run_id] = loaded
    return loaded


def run_label(summary: RunSummary) -> str:
    return (
        f"{status_label(summary.status)} · {summary.factor} · {summary.baseline} · "
        f"{fmt_dt(summary.created_at)} · {summary.run_id[:8]}"
    )


def pick_run(service: DemoService, *, key: str) -> BacktestResult | None:
    """Select a saved run. Reading a saved run never executes research."""
    summaries = service.list_runs()
    if not summaries:
        st.info("No saved runs yet. Build and run a strategy on the *Strategy builder* screen.")
        return None
    by_id = {summary.run_id: summary for summary in summaries}
    preferred = st.session_state.get("selected_run_id")
    ids = list(by_id)
    index = ids.index(preferred) if preferred in by_id else 0
    chosen = st.selectbox(
        "Saved run",
        ids,
        index=index,
        format_func=lambda rid: run_label(by_id[rid]),
        key=f"{key}_run_select",
    )
    with st.expander("Open a run by ID"):
        typed = (
            st.text_input("Run ID (32 hex characters)", key=f"{key}_run_id_input").strip().lower()
        )
        if typed:
            if not _RUN_ID.fullmatch(typed):
                st.error("A run ID is exactly 32 lowercase hexadecimal characters.")
            elif typed not in by_id and service.load_run(typed) is None:
                st.error("No saved run has that ID in this demo database.")
            else:
                chosen = typed
    st.session_state["selected_run_id"] = chosen
    result = load_run_cached(service, chosen)
    if result is None:
        st.error("That run could not be loaded from the demo database.")
    return result
