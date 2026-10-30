"""Shared presentation helpers for the Streamlit demo.

Display only: formatting, banners and small widgets. No research logic, no
database access and no unescaped user text.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

import streamlit as st

from equitytrace.demo.data import SYNTHETIC_NOTICE
from equitytrace.demo.explain import GENERIC_LIMITATIONS, METRIC_HELP
from equitytrace.demo.views import MetricCell
from equitytrace.reporting.html import HYPOTHETICAL_NOTICE

SCREENS: tuple[str, ...] = (
    "Overview",
    "Data explorer",
    "Strategy builder",
    "Results",
    "Inspect decisions",
    "Reports & exports",
    "Methodology & guide",
)
STATUS_ICON = {"success": "✅", "unavailable": "⚠️", "failed": "❌", "running": "⏳"}


def fmt_pct(value: float | None, digits: int = 2) -> str:
    return "Unavailable" if value is None else f"{value:+.{digits}%}"


def fmt_num(value: float | None, digits: int = 4) -> str:
    return "Unavailable" if value is None else f"{value:,.{digits}f}"


def fmt_money_m(value: float | None) -> str:
    return "—" if value is None else f"${value / 1e6:,.1f}M"


def fmt_dt(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def fmt_date(value: date | None) -> str:
    return "—" if value is None else value.isoformat()


def format_metric(cell: MetricCell) -> str:
    if cell.value is None:
        return "Unavailable"
    if cell.kind == "pct":
        return fmt_pct(cell.value)
    if cell.kind == "pct_plain":
        return "Unavailable" if cell.value is None else f"{cell.value:.2%}"
    if cell.kind == "count":
        return f"{int(cell.value)}"
    return fmt_num(cell.value)


def synthetic_banner() -> None:
    st.warning(SYNTHETIC_NOTICE, icon="🧪")


def hypothetical_notice() -> None:
    st.info(HYPOTHETICAL_NOTICE, icon=":material/info:")


def limitations_list() -> None:
    for item in GENERIC_LIMITATIONS:
        st.markdown(f"- {item}")


def metric_grid(cells: Sequence[MetricCell], columns: int = 3) -> None:
    for start in range(0, len(cells), columns):
        row = st.columns(columns)
        for column, cell in zip(row, cells[start : start + columns], strict=False):
            with column:
                help_text = METRIC_HELP.get(cell.key)
                if cell.value is None and cell.unavailable_reason:
                    help_text = cell.unavailable_reason
                st.metric(cell.label, format_metric(cell), help=help_text)


def status_label(status: str) -> str:
    return f"{STATUS_ICON.get(status, '•')} {status.upper()}"


def table(
    rows: list[dict[str, Any]], *, empty: str = "No rows to show.", height: int | None = None
) -> None:
    """Bounded table; values are shown as data, never interpreted as HTML."""
    if not rows:
        st.caption(empty)
        return
    if height is None:
        st.dataframe(rows[:500], hide_index=True, width="stretch")
    else:
        st.dataframe(rows[:500], hide_index=True, width="stretch", height=height)
    if len(rows) > 500:
        st.caption(f"Showing the first 500 of {len(rows)} rows.")


def goto(screen: str) -> Callable[[], None]:
    def callback() -> None:
        st.session_state["screen"] = screen

    return callback


def goto_button(label: str, screen: str, *, key: str, primary: bool = False) -> None:
    st.button(
        label,
        key=key,
        on_click=goto(screen),
        type="primary" if primary else "secondary",
    )
