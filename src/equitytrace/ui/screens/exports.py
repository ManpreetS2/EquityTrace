"""Screen F — downloads regenerated from the persisted run (never re-running research)."""

from __future__ import annotations

import hashlib

import streamlit as st

from equitytrace.demo.service import DemoService, strategy_pretty_json
from equitytrace.portfolio.models import BacktestResult
from equitytrace.strategy.models import ResearchStrategy
from equitytrace.ui.components import (
    goto_button,
    hypothetical_notice,
    limitations_list,
    status_label,
    synthetic_banner,
)
from equitytrace.ui.state import pick_run


def strategy_json_for(result: BacktestResult) -> str:
    """Economic strategy fields of a saved run (display name is not persisted)."""
    request = result.request
    strategy = ResearchStrategy(
        name=f"Persisted run {result.run_id[:8]}",
        description="Economic fields recovered from the saved request; original name not stored.",
        tickers=request.tickers,
        factor=request.factor,
        top_n=request.top_n,
        baseline=request.baseline,
        start_date=request.start_date,
        end_date=request.end_date,
        schedule=request.schedule,
        explicit_dates=request.explicit_dates,
        calendar_symbol=request.calendar_symbol,
        benchmark_symbol=request.benchmark_symbol,
        cost_bps=request.cost_bps,
        initial_nav=request.initial_nav,
        provider=request.provider,
        adjustment_mode=request.adjustment_mode,
    )
    return strategy_pretty_json(strategy)


def render(service: DemoService) -> None:
    st.header("Reports & exports")
    synthetic_banner()
    hypothetical_notice()
    if not service.status().prepared:
        st.info("Load the synthetic demo on the *Overview* screen first.")
        goto_button("Go to Overview", "Overview", key="exp_goto_overview", primary=True)
        return
    result = pick_run(service, key="exports")
    if result is None:
        return
    st.caption(
        f"Run `{result.run_id}` · {status_label(result.status.value)}. Files are regenerated "
        "from the saved run; no research is executed."
    )
    files = {
        "report": (
            "Standalone HTML report",
            DemoService.report_html(result),
            "report.html",
            "text/html",
        ),
        "request": (
            "request.json",
            DemoService.request_json(result),
            "request.json",
            "application/json",
        ),
        "result": (
            "result.json",
            DemoService.result_json(result),
            "result.json",
            "application/json",
        ),
        "strategy": (
            "Strategy JSON",
            strategy_json_for(result),
            "strategy.json",
            "application/json",
        ),
    }
    columns = st.columns(len(files))
    for column, (key, (label, content, suffix, mime)) in zip(columns, files.items(), strict=True):
        data = content.encode("utf-8")
        with column:
            st.download_button(
                f"Download {label}",
                data=data,
                file_name=DemoService.download_name(result, suffix),
                mime=mime,
                key=f"exp_dl_{key}",
            )
            st.caption(f"{len(data):,} bytes")
            st.caption(f"SHA-256 `{hashlib.sha256(data).hexdigest()[:16]}…`")
    st.caption(
        "Regenerating a file from the same saved run produces identical bytes. Two separate "
        "runs of the same strategy have different run IDs and timestamps, so their result.json "
        "files differ even though the economic fingerprint matches."
    )
    st.subheader("What every export carries")
    limitations_list()
