"""Screen E — inspect each rebalance decision and its evidence."""

from __future__ import annotations

import streamlit as st

from equitytrace.demo.service import DemoService
from equitytrace.demo.views import decision_detail, decision_vs_effective_text
from equitytrace.portfolio.models import RebalanceStatus
from equitytrace.ui.components import fmt_dt, goto_button, status_label, synthetic_banner, table
from equitytrace.ui.state import pick_run


def render(service: DemoService) -> None:
    st.header("Inspect decisions")
    synthetic_banner()
    if not service.status().prepared:
        st.info("Load the synthetic demo on the *Overview* screen first.")
        goto_button("Go to Overview", "Overview", key="insp_goto_overview", primary=True)
        return
    result = pick_run(service, key="inspect")
    if result is None:
        return
    st.caption(f"Run `{result.run_id}` · {status_label(result.status.value)}")
    if not result.rebalances:
        st.warning(
            f"This run has no rebalance records (status {result.status.value}, reason "
            f"`{result.failure_reason}`). Nothing was decided, so there is nothing to inspect.",
            icon="⚠️",
        )
        return

    labels = [
        f"#{i + 1} · decision {fmt_dt(r.decision_at)} · {r.status.value}"
        for i, r in enumerate(result.rebalances)
    ]
    index = st.selectbox(
        "Rebalance", range(len(labels)), format_func=lambda i: labels[i], key="insp_idx"
    )
    rebalance = result.rebalances[index]
    detail = decision_detail(rebalance)

    st.info(
        "**Decision time and effective time are different events.** "
        + decision_vs_effective_text(rebalance),
        icon="🕒",
    )
    cols = st.columns(4)
    cols[0].metric("Status", rebalance.status.value.upper())
    cols[1].metric("Gross turnover", f"{rebalance.gross_turnover:.4f}")
    cols[2].metric("Cost (NAV units)", f"{rebalance.cost_amount:.6f}")
    cols[3].metric(
        "NAV pre → post cost", f"{rebalance.pre_cost_nav:.4f} → {rebalance.post_cost_nav:.4f}"
    )

    if rebalance.status is not RebalanceStatus.SUCCESS:
        st.warning(
            f"This rebalance was **{rebalance.status.value}** (`{rebalance.reason}`); the "
            "portfolio kept its prior weights and no costs were charged.",
            icon="⚠️",
        )
        return

    st.subheader("Selected names, ranks and weights")
    table(detail.rows)
    st.caption(
        "Drifted weight = the weight just before this rebalance after returns since the last "
        "one. Turnover notional is in NAV units, not shares: EquityTrace models weight "
        "transitions and does not create fills, share counts or prices paid."
    )
    for note in detail.tie_notes:
        st.warning(note)
    st.subheader("Signal evidence")
    table(detail.evidence, empty="No signal provenance was stored for this rebalance.")
    filings = service.filings_by_accession(detail.accessions)
    if filings:
        st.subheader("Source filings and when they became public")
        decision_at = rebalance.decision_at
        table(
            [
                {
                    "Accession": f.accession,
                    "Form": f.form,
                    "Period end": f.report_date.isoformat() if f.report_date else "—",
                    "Public at": fmt_dt(f.available_at),
                    "Public by decision?": "YES" if f.available_at <= decision_at else "NO — LEAK",
                }
                for f in filings
            ]
        )
    if result.audit is not None:
        st.caption(
            "Run-level audit: "
            + ("passed" if result.audit.passed else "FAILED — " + "; ".join(result.audit.failures))
        )
