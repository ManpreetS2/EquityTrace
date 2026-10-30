"""Screen D — results of a saved run, read from persistence only."""

from __future__ import annotations

import streamlit as st

from equitytrace.demo.explain import WARNING_TEXT, explain_reason
from equitytrace.demo.service import DemoService
from equitytrace.demo.views import (
    allocation_rows,
    equity_rows,
    metric_cells,
    rebalance_summary_rows,
)
from equitytrace.factors.registry import UnknownFactorError, get_factor
from equitytrace.portfolio.models import BacktestResult, PortfolioRunStatus
from equitytrace.strategy.models import fingerprint_request
from equitytrace.ui.components import (
    fmt_date,
    goto_button,
    hypothetical_notice,
    metric_grid,
    status_label,
    synthetic_banner,
    table,
)
from equitytrace.ui.state import pick_run


def run_header(service: DemoService, result: BacktestResult) -> None:
    request = result.request
    status = result.status
    if status is PortfolioRunStatus.SUCCESS:
        st.success(f"Research status: {status_label(status.value)}", icon="✅")
    elif status is PortfolioRunStatus.UNAVAILABLE:
        st.warning(
            f"Research status: {status_label(status.value)} — no performance exists.", icon="⚠️"
        )
    else:
        st.error(
            f"Research status: {status_label(status.value)} — no performance is shown.", icon="❌"
        )
    manifest = service.status()
    direction: str
    try:
        direction = get_factor(request.factor).ranking_direction
    except UnknownFactorError:
        direction = "unknown"
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Run ID** `{result.run_id}`")
        st.markdown(f"**Economic fingerprint** `{fingerprint_request(request)}`")
        st.markdown(f"**Dataset** SYNTHETIC · `{manifest.dataset_id}` v{manifest.dataset_version}")
        st.markdown(f"**Package version** `{result.package_version}`")
    with right:
        st.markdown(f"**Universe** {', '.join(result.universe or request.tickers)}")
        st.markdown(f"**Factor** `{request.factor}` ({direction}) · **Top-N** {request.top_n}")
        st.markdown(
            f"**Baseline** `{request.baseline.value}` · **Schedule** `{request.schedule.value}` · "
            f"**Costs** {request.cost_bps:g} bps"
        )
        st.markdown(
            f"**Dates** {fmt_date(request.start_date)} → {fmt_date(request.end_date)} · "
            f"**Benchmark** {request.benchmark_symbol or 'none'} · "
            f"**Calendar** {request.calendar_symbol}"
        )
    if status is not PortfolioRunStatus.SUCCESS:
        st.markdown(f"**Reason:** `{result.failure_reason}`")
        st.write(explain_reason(result.failure_reason))


def render(service: DemoService) -> None:
    st.header("Results")
    synthetic_banner()
    if not service.status().prepared:
        st.info("Load the synthetic demo on the *Overview* screen first.")
        goto_button("Go to Overview", "Overview", key="res_goto_overview", primary=True)
        return
    result = pick_run(service, key="results")
    if result is None:
        goto_button("Build a strategy", "Strategy builder", key="res_goto_strategy", primary=True)
        return

    run_header(service, result)
    hypothetical_notice()
    if result.status is not PortfolioRunStatus.SUCCESS:
        st.caption("Failed and unavailable runs never display NAV, return or drawdown figures.")
        _warnings(result)
        return

    st.subheader("Performance (read from the saved run)")
    metric_grid(metric_cells(result))
    rows = equity_rows(result)
    st.subheader("Portfolio NAV vs benchmark")
    series = ["Portfolio NAV"]
    if any(row["Benchmark NAV"] is not None for row in rows):
        series.append("Benchmark NAV")
    else:
        st.caption("No benchmark series is available for this run.")
    st.line_chart(rows, x="date", y=series)
    st.subheader("Drawdown")
    st.area_chart(rows, x="date", y="Drawdown")
    allocations = allocation_rows(result)
    st.subheader("Target allocation at each rebalance")
    if allocations:
        st.caption(
            "Target weights set at each successful rebalance's effective time. Weights then "
            "drift with returns between rebalances; daily drifted weights are not stored."
        )
        st.bar_chart(allocations, x="effective", y="target_weight", color="symbol")
    else:
        st.caption("No successful rebalance, so there is no allocation to chart.")
    st.subheader("Rebalances")
    table(rebalance_summary_rows(result), height=320)
    _warnings(result)
    goto_button("Inspect individual decisions", "Inspect decisions", key="res_goto_inspect")


def _warnings(result: BacktestResult) -> None:
    st.subheader("Warnings and audit")
    for warning in result.warnings:
        st.markdown(f"- **{warning}** — {WARNING_TEXT.get(warning, 'See the methodology screen.')}")
    audit = result.audit
    if audit is None:
        st.caption("No leakage audit was recorded for this run.")
    elif audit.passed:
        st.success(
            "Point-in-time leakage audit passed: no decision used evidence from its future.",
            icon="🛡️",
        )
    else:
        st.error("Leakage audit FAILED: " + "; ".join(audit.failures))
