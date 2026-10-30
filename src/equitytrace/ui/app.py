"""Streamlit entry point: ``streamlit run src/equitytrace/ui/app.py``.

A thin, local-only presentation layer. It prepares synthetic data and runs a
backtest only in response to explicit button clicks; every other rerun is a pure
read of already persisted state.
"""

from __future__ import annotations

import streamlit as st

from equitytrace.ui.components import SCREENS
from equitytrace.ui.screens import (
    decisions,
    explorer,
    exports,
    methodology,
    overview,
    results,
    strategy,
)
from equitytrace.ui.state import get_service

RENDERERS = {
    "Overview": overview.render,
    "Data explorer": explorer.render,
    "Strategy builder": strategy.render,
    "Results": results.render,
    "Inspect decisions": decisions.render,
    "Reports & exports": exports.render,
    "Methodology & guide": methodology.render,
}


def main() -> None:
    st.set_page_config(
        page_title="EquityTrace — Transparent Equity Research",
        page_icon="📈",
        layout="wide",
    )
    st.session_state.setdefault("screen", SCREENS[0])
    service = get_service()
    with st.sidebar:
        st.markdown("### EquityTrace")
        st.caption("Transparent Equity Research · local offline demo")
        st.radio("Navigate", SCREENS, key="screen")
        st.divider()
        st.caption("🧪 All data is synthetic and fictional.")
        st.caption("Research tool only: not a trading application and not investment advice.")
    notice = st.session_state.pop("prepare_message", None)
    if notice:
        st.toast(str(notice))
    RENDERERS[str(st.session_state["screen"])](service)


if __name__ == "__main__":
    main()
