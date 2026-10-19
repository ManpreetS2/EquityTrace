"""Standalone research reports and deterministic JSON exports (v1.0 Milestone 2).

Reads persisted ``BacktestResult`` rows via ``PortfolioRepository``. Does not run
the engine, ingest market data, or mutate stored research.
"""

from __future__ import annotations

from equitytrace.reporting.export import (
    ReportingError,
    export_run_json,
    render_request_export,
    render_result_export,
)
from equitytrace.reporting.html import render_research_report

__all__ = [
    "ReportingError",
    "export_run_json",
    "render_request_export",
    "render_research_report",
    "render_result_export",
]
