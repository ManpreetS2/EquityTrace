"""Screen B — financial data explorer with point-in-time as-of selection."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import streamlit as st

from equitytrace.demo.data import PRICE_END, PRICE_START
from equitytrace.demo.service import DemoService, SecurityRow, decision_time
from equitytrace.factors.models import FactorResult
from equitytrace.factors.registry import list_factors
from equitytrace.financials.models import CanonicalValue
from equitytrace.ui.components import (
    fmt_dt,
    fmt_money_m,
    fmt_num,
    goto_button,
    synthetic_banner,
    table,
)

DEFAULT_ASOF = date(2024, 3, 20)
# SYBR's FY2023 10-K is accepted 2024-03-14 16:30 Eastern: the cleanest availability example.
EXAMPLE = ("SYBR", date(2024, 3, 13), date(2024, 3, 15), 2023)


def _use_example() -> None:
    ticker, before, after, year = EXAMPLE
    st.session_state.update(
        {"ex_ticker": ticker, "cmp_a": before, "cmp_b": after, "cmp_fy": year, "ex_asof": after}
    )


def _value_text(item: CanonicalValue) -> str:
    if item.value is None:
        return "Unavailable"
    if item.unit == "USD":
        return fmt_money_m(item.value)
    return fmt_num(item.value, 4)


def _statement_rows(section: dict[str, CanonicalValue]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, item in section.items():
        prov = item.provenance
        rows.append(
            {
                "Metric": name,
                "Value": _value_text(item),
                "Source concept": prov.source_concept or "—",
                "Accession": prov.accession_number or "—",
                "Form": prov.form or "—",
                "Period": (
                    f"{prov.period_start or '…'} → {prov.period_end}" if prov.period_end else "—"
                ),
                "Filed": prov.filing_date.isoformat() if prov.filing_date else "—",
                "Public at": fmt_dt(prov.available_at),
                "Derived": f"yes: {prov.derivation}" if prov.is_derived else "no",
                "Note": item.missing_reason or ", ".join(item.warnings),
            }
        )
    return rows


def _factor_rows(factors: list[FactorResult]) -> list[dict[str, Any]]:
    return [
        {
            "Factor": item.factor,
            "Direction": item.ranking_direction,
            "Value": None if item.value is None else round(item.value, 6),
            "Status": "valid" if item.valid else "UNAVAILABLE",
            "Why unavailable": item.unavailable_reason or "",
            "Inputs": json.dumps(item.inputs, sort_keys=True),
            "Source filings": ", ".join(item.source_filings),
            "Warnings": ", ".join(item.warnings),
        }
        for item in factors
    ]


def render(service: DemoService) -> None:
    st.header("Data explorer")
    synthetic_banner()
    if not service.status().prepared:
        st.info("Load the synthetic demo on the *Overview* screen first.")
        goto_button("Go to Overview", "Overview", key="ex_goto_overview", primary=True)
        return

    st.session_state.setdefault("ex_asof", DEFAULT_ASOF)
    st.session_state.setdefault("cmp_a", EXAMPLE[1])
    st.session_state.setdefault("cmp_b", EXAMPLE[2])
    st.session_state.setdefault("cmp_fy", EXAMPLE[3])
    securities = service.securities()
    by_ticker: dict[str, SecurityRow] = {item.ticker: item for item in securities}
    left, right = st.columns([2, 1])
    with left:
        ticker = st.selectbox(
            "Fictional security",
            list(by_ticker),
            format_func=lambda t: f"{t} — {by_ticker[t].issuer_name}",
            key="ex_ticker",
        )
    with right:
        day = st.date_input(
            "As-of date (16:15 Eastern, the engine's decision-time convention)",
            min_value=PRICE_START,
            max_value=PRICE_END,
            key="ex_asof",
        )
    as_of = decision_time(day)
    row = by_ticker[ticker]
    st.caption(
        f"Issuer CIK {row.cik} (fictional range) · {row.securities_for_issuer} security(ies) for "
        f"this issuer · {service.company_story(ticker) or ''} · as-of {fmt_dt(as_of)}"
    )
    if row.securities_for_issuer > 1:
        st.warning(
            "This issuer has more than one share class. Fundamentals are issuer-level, so a "
            "strategy cannot hold two classes of it; the engine will refuse rather than choose.",
            icon="🪪",
        )

    tabs = st.tabs(
        ["Statements & provenance", "Factor values", "Filings", "Cross-section", "As-of comparison"]
    )
    periods = service.available_periods(ticker, as_of)

    with tabs[0]:
        if not periods:
            st.warning(
                f"No annual report from {ticker} is public as of {fmt_dt(as_of)}, so no "
                "statement values exist yet. Move the as-of date later.",
                icon="⏳",
            )
        else:
            labels = {p.fiscal_year: p.label() for p in periods}
            year = st.selectbox(
                "Fiscal period (newest public first)",
                list(labels),
                format_func=lambda y: labels[y],
                key="ex_period",
            )
            view = service.snapshot_view(ticker, year, as_of)
            snapshot = view.snapshot
            st.caption(f"Source accessions: {', '.join(snapshot.source_accessions) or 'none'}")
            if snapshot.missing:
                with st.expander(f"{len(snapshot.missing)} canonical metrics have no data"):
                    st.write(
                        "The synthetic filings report only the concepts needed by the demo "
                        "factors, so these metrics are Unavailable rather than guessed: "
                        + ", ".join(snapshot.missing)
                    )
            for title, section in (
                ("Income statement", snapshot.income_statement),
                ("Balance sheet", snapshot.balance_sheet),
                ("Cash flow", snapshot.cash_flow),
                ("Derived metrics (with derivation lineage)", snapshot.derived),
            ):
                with st.expander(title, expanded=title == "Income statement"):
                    table(_statement_rows(section))
            with tabs[1]:
                st.caption(
                    "Computed by the registered factor formulas for this period and as-of. "
                    "Valuation factors need market capitalization from raw prices and share "
                    "counts; the demo has neither, so they are Unavailable by design."
                )
                table(_factor_rows(view.factors))
    if not periods:
        with tabs[1]:
            st.info("Factor values appear once an annual report is public.")

    with tabs[2]:
        st.caption("A filing is usable only from its public-availability time (after acceptance).")
        table(
            [
                {
                    "Accession": f.accession,
                    "Form": f.form,
                    "Period end": f.report_date.isoformat() if f.report_date else "—",
                    "Filed": f.filing_date.isoformat(),
                    "Public at": fmt_dt(f.available_at),
                    "Public at this as-of?": "YES" if f.public_at_as_of else "not yet",
                    "Source": f.source_url or "",
                }
                for f in service.filings(ticker, as_of)
            ]
        )

    with tabs[3]:
        factor = st.selectbox(
            "Factor", list_factors(), index=list_factors().index("roa"), key="ex_factor"
        )
        singles = [s.ticker for s in securities if s.securities_for_issuer == 1 or s.is_primary]
        ranked = service.ranking_at(singles, factor, as_of)
        st.caption(
            "Each name uses its own newest public annual report, exactly as the backtest does. "
            "Names with no usable value are listed with the reason, not ranked."
        )
        table(
            [
                {
                    "Rank": item.rank,
                    "Ticker": item.ticker,
                    "Value": (
                        None
                        if item.result is None or item.result.value is None
                        else round(item.result.value, 6)
                    ),
                    "Period": "—" if item.result is None else item.result.period.label(),
                    "Direction": "—" if item.result is None else item.result.ranking_direction,
                    "Why unavailable": item.reason or "",
                }
                for item in ranked
            ]
        )

    with tabs[4]:
        st.write(
            "Compare the same statement at two as-of dates to watch a filing become public. "
            "Example: SYBR's FY2023 10-K is accepted on 2024-03-14 after the close."
        )
        st.button("Use the SYBR example", on_click=_use_example, key="cmp_example")
        col_a, col_b, col_y = st.columns(3)
        with col_a:
            day_a = st.date_input(
                "As-of A", min_value=PRICE_START, max_value=PRICE_END, key="cmp_a"
            )
        with col_b:
            day_b = st.date_input(
                "As-of B", min_value=PRICE_START, max_value=PRICE_END, key="cmp_b"
            )
        with col_y:
            year_c = st.selectbox("Fiscal year", [2023, 2022, 2021], key="cmp_fy")
        snap_a = service.snapshot_view(
            ticker, year_c, decision_time(day_a), with_factors=False
        ).snapshot
        snap_b = service.snapshot_view(
            ticker, year_c, decision_time(day_b), with_factors=False
        ).snapshot
        rows: list[dict[str, Any]] = []
        for section_a, section_b in (
            (snap_a.income_statement, snap_b.income_statement),
            (snap_a.balance_sheet, snap_b.balance_sheet),
            (snap_a.cash_flow, snap_b.cash_flow),
        ):
            for name, item_b in section_b.items():
                item_a = section_a[name]
                if item_a.value is None and item_b.value is None:
                    continue
                rows.append(
                    {
                        "Metric": name,
                        f"At {day_a.isoformat()}": _value_text(item_a),
                        f"At {day_b.isoformat()}": _value_text(item_b),
                        "Newly public accession": (
                            item_b.provenance.accession_number if item_a.value is None else ""
                        ),
                        "Public at": fmt_dt(item_b.provenance.available_at),
                    }
                )
        if rows:
            table(rows)
        else:
            st.info(f"No FY{year_c} values are public at either date for {ticker}.")
