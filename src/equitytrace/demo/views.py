"""Pure, database-free views of a persisted ``BacktestResult``.

The UI renders these rows; it never recomputes a headline metric. Everything is
read straight from the persisted result (weights, costs, ranks, provenance).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from equitytrace.portfolio.models import (
    BacktestResult,
    PerformanceMetrics,
    PortfolioRunStatus,
    RebalanceRecord,
)


@dataclass(frozen=True, slots=True)
class MetricCell:
    key: str
    label: str
    value: float | None
    kind: str  # "pct" | "number" | "count"
    unavailable_reason: str | None


def metric_cells(result: BacktestResult) -> list[MetricCell]:
    """Headline metrics; a missing value carries its reason instead of a zero."""
    metrics: PerformanceMetrics | None = result.metrics
    if result.status is not PortfolioRunStatus.SUCCESS or metrics is None:
        not_run = f"Run status is {result.status.value}; no performance was produced."
        return [
            MetricCell(key, label, None, kind, not_run)
            for key, label, kind in _METRIC_LAYOUT
            if key != "rebalances"
        ]
    intervals = metrics.interval_count
    short = f"Needs more sessions (this run has {intervals} daily intervals)."
    cells: list[MetricCell] = []
    for key, label, kind in _METRIC_LAYOUT:
        value: float | None
        reason: str | None
        if key == "final_nav":
            value, reason = result.final_nav, "Final NAV missing from the persisted result."
        elif key == "rebalances":
            value, reason = float(metrics.successful_rebalance_count), None
        else:
            value = getattr(metrics, key)
            reason = {
                "annualized_return": short,
                "annualized_volatility": short,
                "sharpe_ratio": "Undefined: needs ≥ 2 daily intervals and non-zero variance.",
            }.get(key, "Not computed for this run.")
        cells.append(MetricCell(key, label, value, kind, None if value is not None else reason))
    return cells


_METRIC_LAYOUT: tuple[tuple[str, str, str], ...] = (
    ("final_nav", "Final NAV", "number"),
    ("total_return", "Total return", "pct"),
    ("annualized_return", "Annualized return", "pct"),
    ("annualized_volatility", "Volatility (ann.)", "pct_plain"),
    ("sharpe_ratio", "Sharpe (rf = 0)", "number"),
    ("max_drawdown", "Max drawdown", "pct"),
    ("gross_turnover", "Gross turnover", "number"),
    ("transaction_cost_total", "Transaction costs (NAV)", "number"),
    ("rebalances", "Successful rebalances", "count"),
)


def equity_rows(result: BacktestResult) -> list[dict[str, Any]]:
    """Persisted equity curve, one row per valuation session."""
    return [
        {
            "valuation_at": point.valuation_at,
            "date": point.valuation_at.date(),
            "Portfolio NAV": point.nav,
            "Benchmark NAV": point.benchmark_nav,
            "Drawdown": point.drawdown,
            "Cash weight": point.cash_weight,
        }
        for point in result.equity
    ]


def allocation_rows(result: BacktestResult) -> list[dict[str, Any]]:
    """Target weights after each successful rebalance (not daily drifted weights)."""
    rows: list[dict[str, Any]] = []
    for rebalance in result.rebalances:
        if rebalance.status.value != "success":
            continue
        for item in rebalance.transitions:
            rows.append(
                {
                    "effective": rebalance.target_effective_at,
                    "symbol": item.symbol,
                    "target_weight": item.target_weight_after,
                }
            )
    return rows


def rebalance_summary_rows(result: BacktestResult) -> list[dict[str, Any]]:
    return [
        {
            "#": index + 1,
            "Decision (UTC)": rebalance.decision_at,
            "Target effective (UTC)": rebalance.target_effective_at,
            "Status": rebalance.status.value,
            "Reason": rebalance.reason or "",
            "Selected": ", ".join(
                f"{t.symbol}" for t in rebalance.transitions if t.target_weight_after > 0
            ),
            "Gross turnover": rebalance.gross_turnover,
            "Cost (NAV)": rebalance.cost_amount,
            "Pre-cost NAV": rebalance.pre_cost_nav,
            "Post-cost NAV": rebalance.post_cost_nav,
        }
        for index, rebalance in enumerate(result.rebalances)
    ]


@dataclass(frozen=True, slots=True)
class DecisionDetail:
    rebalance: RebalanceRecord
    rows: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    tie_notes: list[str]
    accessions: tuple[str, ...]


def decision_detail(rebalance: RebalanceRecord) -> DecisionDetail:
    """Per-name rows and signal evidence for one persisted rebalance."""
    rows: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    tie_notes: list[str] = []
    accessions: list[str] = []
    for item in rebalance.transitions:
        provenance = _provenance(item.signal_provenance_json)
        filings = [str(a) for a in provenance.get("source_filings", [])]
        accessions.extend(filings)
        rows.append(
            {
                "Symbol": item.symbol,
                "Rank": item.signal_rank,
                "Factor value": item.signal_value,
                "Report period": item.signal_period,
                "Drifted weight before": item.current_weight_before,
                "Target weight after": item.target_weight_after,
                "Weight change": item.delta_weight,
                "Turnover notional (NAV units)": item.gross_notional,
                "Allocated cost (NAV)": item.allocated_cost,
            }
        )
        if provenance:
            evidence.append(
                {
                    "Symbol": item.symbol,
                    "Factor": provenance.get("factor"),
                    "Ranking direction": provenance.get("ranking_direction"),
                    "As-of (decision)": provenance.get("as_of"),
                    "Inputs": json.dumps(provenance.get("inputs", {}), sort_keys=True),
                    "Source filings": ", ".join(filings),
                    "Warnings": ", ".join(provenance.get("warnings", [])),
                }
            )
            tie = provenance.get("top_n_tie_at_cutoff")
            if tie:
                tie_notes.append(
                    f"Tie at cutoff ({tie.get('rule')}): tied {tie.get('tied_tickers')}, "
                    f"selected {tie.get('selected_tickers')}, "
                    f"excluded {tie.get('excluded_tickers')}."
                )
    return DecisionDetail(
        rebalance=rebalance,
        rows=rows,
        evidence=evidence,
        tie_notes=list(dict.fromkeys(tie_notes)),
        accessions=tuple(dict.fromkeys(accessions)),
    )


def _provenance(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def decision_vs_effective_text(rebalance: RebalanceRecord) -> str:
    gap = rebalance.target_effective_at - rebalance.decision_at
    return (
        f"Decided at {_stamp(rebalance.decision_at)} using only evidence public by then; "
        f"the target weights take effect at {_stamp(rebalance.target_effective_at)} "
        f"({gap} later). Weight changes are modelled transitions, not broker fills."
    )


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")
