"""Screen G — guided walkthrough backed by live calls to the real services."""

from __future__ import annotations

from datetime import date
from typing import Any

import streamlit as st

from equitytrace.demo.service import DemoService, decision_time
from equitytrace.ui.components import (
    fmt_dt,
    fmt_money_m,
    goto_button,
    limitations_list,
    synthetic_banner,
    table,
)

PIT_TICKER = "SYBR"
PIT_DATES = (date(2024, 3, 13), date(2024, 3, 14), date(2024, 3, 15))


def render(service: DemoService) -> None:
    st.header("Methodology & guided walkthrough")
    synthetic_banner()
    st.markdown(
        "**Architecture:** synthetic fixture → existing repositories and services → "
        "`ResearchStrategy` → `BacktestEngine` → persisted run → Streamlit / HTML / JSON. "
        "The CLI uses the same models and services; Streamlit is one presentation layer, not "
        "the only interface."
    )
    ready = service.status().prepared
    if not ready:
        st.info("Load the synthetic demo on the *Overview* screen to see the live examples below.")
        goto_button("Go to Overview", "Overview", key="meth_goto_overview", primary=True)

    with st.expander("1 · Why public availability matters", expanded=True):
        st.markdown(
            "A fiscal year ending 31 December is **not** public on 31 December. EquityTrace "
            "uses each filing's acceptance time as `available_at`, and a decision may use a fact "
            "only if `available_at ≤ decision time`. Below, the same lookup is made at three "
            f"dates around {PIT_TICKER}'s FY2023 10-K acceptance."
        )
        if ready:
            rows: list[dict[str, Any]] = []
            for day in PIT_DATES:
                as_of = decision_time(day)
                periods = service.available_periods(PIT_TICKER, as_of)
                latest = periods[0].label() if periods else "none"
                revenue = "Unavailable"
                if periods:
                    snap = service.snapshot_view(
                        PIT_TICKER, periods[0].fiscal_year, as_of, with_factors=False
                    ).snapshot
                    item = snap.income_statement.get("revenue")
                    if item is not None and item.value is not None:
                        revenue = fmt_money_m(item.value)
                rows.append(
                    {
                        "As-of": f"{day.isoformat()} 16:15 ET ({fmt_dt(as_of)})",
                        "Newest public annual period": latest,
                        "Its revenue": revenue,
                    }
                )
            table(rows)
            st.caption(
                "FY2023 appears only after the 2024-03-14 16:30 ET acceptance: not on the 13th, "
                "and not on the 14th at the 16:15 decision time, only from the 15th."
            )
            goto_button(
                "Explore this in the data explorer", "Data explorer", key="meth_go_explorer"
            )

    with st.expander("2 · How facts become canonical metrics"):
        st.markdown(
            "Each stored fact keeps its XBRL concept, unit, period, accession, form and "
            "`available_at`. The canonical layer picks one fact per metric using fixed "
            "rules (annual spans, 10-K preferred, latest period), keeps the provenance, and "
            "derives ratios with an explicit derivation string. Missing inputs yield "
            "*Unavailable with a reason*, never a zero."
        )
    with st.expander("3 · How factors produce rankings"):
        st.markdown(
            "A factor is a transparent formula over canonical metrics (for example ROA = net "
            "income ÷ average total assets). Each factor declares its ranking direction. "
            "Because companies file on different days, a cross-section can mix fiscal years "
            "for a few weeks; the engine reports that as the `mixed_fiscal_periods` warning."
        )
        if ready:
            columns = st.columns(2)
            for column, day in zip(columns, (date(2024, 2, 29), date(2024, 3, 29)), strict=True):
                with column:
                    st.markdown(f"**ROA ranking at {day.isoformat()}**")
                    ranked = service.ranking_at(
                        ["SYAL", "SYBR", "SYCD", "SYDL", "SYEN", "SYFM", "SYGA"],
                        "roa",
                        decision_time(day),
                    )
                    table(
                        [
                            {
                                "Rank": item.rank,
                                "Ticker": item.ticker,
                                "ROA": None if item.result is None else item.result.value,
                                "Period": "—"
                                if item.result is None
                                else item.result.period.label(),
                            }
                            for item in ranked
                        ]
                    )
    with st.expander("4 · How strategies select securities"):
        st.markdown(
            "A `ResearchStrategy` fixes the universe, factor, top-N, weighting method, dates, "
            "schedule, costs and benchmark. The engine takes each name's newest public annual "
            "factor value at the decision time, ranks them, and keeps exactly the top N (ties "
            "broken by ticker, with a warning). Fewer than N valid names makes the run "
            "*unavailable* instead of padding the portfolio."
        )
    with st.expander("5 · How the portfolio evolves"):
        st.markdown(
            "Decisions are taken at a session's close; the new target weights take effect at "
            "the **next** session. Between rebalances, weights drift with each holding's "
            "return and cash earns nothing. Missing prices for a held name fail the run "
            "rather than assuming zero return."
        )
    with st.expander("6 · Where transaction costs appear"):
        st.markdown(
            "Cost = NAV x gross turnover x cost rate, charged only when weights actually "
            "change. Gross turnover is Σ|target - drifted weight|. The *Inspect decisions* "
            "screen shows each name's allocated share of the cost."
        )
    with st.expander("7 · Why backtest assumptions matter"):
        limitations_list()
        st.markdown(
            "In this dataset prices are generated **independently of the fundamentals**, so "
            "no factor has a built-in edge. A strategy can and often will lose money or trail "
            "the benchmark. That is the honest outcome, not a bug."
        )
    with st.expander("8 · How to inspect the complete evidence trail"):
        st.markdown(
            "Results → *Inspect decisions* → pick a rebalance → read the per-name rank, factor "
            "value, inputs and source accessions → compare each filing's *Public at* time with "
            "the decision time. *Reports & exports* downloads the same evidence as HTML and "
            "deterministic JSON."
        )
