"""Self-contained HTML research reports from persisted BacktestResult rows."""

from __future__ import annotations

import html
import json
import math
from datetime import date, datetime
from typing import Any

from equitytrace.factors.registry import UnknownFactorError, get_factor
from equitytrace.portfolio.models import (
    BacktestResult,
    EquityPoint,
    PerformanceMetrics,
    PortfolioRunStatus,
)
from equitytrace.strategy.models import fingerprint_request

# Visualization-only cap; full equity series remain in JSON exports.
_MAX_CHART_POINTS = 2_000

_METHODOLOGY_LIMITATIONS = (
    "Provider-adjusted price history is not vintage point-in-time price history.",
    "User-supplied universes can contain survivorship bias.",
    "Calendar decisions precede next-session target effectiveness.",
    "Holdings drift and transaction costs are modeled by EquityTrace; "
    "this is not a share/fill broker simulation.",
    "No intraday, order-book, borrow, or short-sale model is applied.",
    "Outputs are reproducible research artifacts, not investment advice.",
)


def render_research_report(result: BacktestResult) -> str:
    """Build a deterministic standalone HTML document for one persisted run."""
    fingerprint = fingerprint_request(result.request)
    status = result.status
    success = status is PortfolioRunStatus.SUCCESS
    body = "\n".join(
        [
            _status_banner(result),
            _section_identity(result, fingerprint),
            _section_configuration(result),
            _section_performance(result, success=success),
            _section_charts(result, success=success),
            _section_rebalances(result),
            _section_transitions(result),
            _section_signals(result),
            _section_audit(result),
            _section_limitations(result),
        ]
    )
    title = f"EquityTrace research report · {_esc(result.run_id)}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<main class="page">\n'
        f'<header class="hero">\n'
        "<h1>EquityTrace</h1>\n"
        '<p class="subtitle">Standalone research report</p>\n'
        "</header>\n"
        f"{body}\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_number(value: float | None, *, digits: int = 6) -> str:
    if value is None or not math.isfinite(value):
        return "Unavailable"
    text = f"{value:.{digits}g}"
    return _esc(text)


def _fmt_pct(value: float | None, *, digits: int = 4) -> str:
    if value is None or not math.isfinite(value):
        return "Unavailable"
    return _esc(f"{value * 100:.{digits}f}%")


def _fmt_ts(value: datetime | None) -> str:
    if value is None:
        return "Unavailable"
    return _esc(value.astimezone().isoformat() if value.tzinfo else value.isoformat())


def _fmt_date(value: date | None) -> str:
    if value is None:
        return "Unavailable"
    return _esc(value.isoformat())


def _status_banner(result: BacktestResult) -> str:
    status = result.status
    if status is PortfolioRunStatus.SUCCESS:
        css = "banner banner-success"
        headline = "Research status: SUCCESS"
    elif status is PortfolioRunStatus.UNAVAILABLE:
        css = "banner banner-unavailable"
        headline = "Research status: UNAVAILABLE"
    elif status is PortfolioRunStatus.FAILED:
        css = "banner banner-failed"
        headline = "Research status: FAILED"
    else:
        css = "banner banner-other"
        headline = f"Research status: {_esc(status.value.upper())}"
    reason = ""
    if result.failure_reason:
        reason = (
            f'<p class="banner-reason">Failure reason: '
            f"<code>{_esc(result.failure_reason)}</code></p>"
        )
    note = ""
    if status is not PortfolioRunStatus.SUCCESS:
        note = (
            '<p class="banner-note">This report describes a non-successful research '
            "result. Missing metrics and charts are unavailable, not zero.</p>"
        )
    return f'<section class="{css}" role="status"><h2>{headline}</h2>{reason}{note}</section>'


def _section_identity(result: BacktestResult, fingerprint: str) -> str:
    rows = [
        ("run_id", f"<code>{_esc(result.run_id)}</code>"),
        ("economic_fingerprint", f"<code>{_esc(fingerprint)}</code>"),
        ("package_version", _esc(result.package_version)),
        ("research_status", _esc(result.status.value)),
        ("failure_reason", _esc(result.failure_reason) if result.failure_reason else "—"),
        ("created_at", _fmt_ts(result.created_at)),
        ("completed_at", _fmt_ts(result.completed_at)),
    ]
    return _section(
        "Research identity",
        "<p>Strategy name and description are not persisted with the run. "
        "Economic identity is the fingerprint of the stored request.</p>" + _kv_table(rows),
    )


def _section_configuration(result: BacktestResult) -> str:
    request = result.request
    direction = _ranking_direction(request.factor)
    rows = [
        ("universe", _esc(", ".join(request.tickers))),
        ("factor", _esc(request.factor)),
        ("ranking_direction", _esc(direction) if direction else "Unavailable"),
        ("top_n", _esc(request.top_n)),
        ("baseline", _esc(request.baseline.value)),
        ("schedule", _esc(request.schedule.value)),
        ("start_date", _fmt_date(request.start_date)),
        ("end_date", _fmt_date(request.end_date)),
        (
            "explicit_dates",
            _esc(", ".join(day.isoformat() for day in request.explicit_dates))
            if request.explicit_dates
            else "—",
        ),
        ("calendar", _esc(request.calendar_symbol)),
        (
            "benchmark",
            _esc(request.benchmark_symbol) if request.benchmark_symbol else "—",
        ),
        ("cost_bps", _fmt_number(request.cost_bps)),
        ("initial_nav", _fmt_number(request.initial_nav)),
        ("provider", _esc(request.provider.value)),
        ("adjustment_mode", _esc(request.adjustment_mode.value)),
    ]
    return _section("Strategy configuration", _kv_table(rows))


def _ranking_direction(factor: str) -> str | None:
    try:
        return get_factor(factor).ranking_direction
    except UnknownFactorError:
        return None


def _section_performance(result: BacktestResult, *, success: bool) -> str:
    if not success:
        return _section(
            "Performance summary",
            '<p class="muted">Performance metrics are not shown as success '
            f"values because research status is <strong>{_esc(result.status.value)}"
            "</strong>.</p>" + _metrics_table(result.metrics, result.final_nav, allow_display=True),
        )
    return _section(
        "Performance summary",
        "<p>Values below are exactly the persisted metrics for this run.</p>"
        + _metrics_table(result.metrics, result.final_nav, allow_display=True),
    )


def _metrics_table(
    metrics: PerformanceMetrics | None,
    final_nav: float | None,
    *,
    allow_display: bool,
) -> str:
    if not allow_display:
        return '<p class="muted">Unavailable</p>'
    if metrics is None and final_nav is None:
        return '<p class="muted">No persisted metrics for this run.</p>'
    rows = [
        ("Final NAV", _fmt_number(final_nav)),
        (
            "Total return",
            _fmt_pct(metrics.total_return if metrics else None),
        ),
        (
            "Annualized return",
            _fmt_pct(metrics.annualized_return if metrics else None),
        ),
        (
            "Annualized volatility",
            _fmt_pct(metrics.annualized_volatility if metrics else None),
        ),
        (
            "Sharpe ratio (rf=0)",
            _fmt_number(metrics.sharpe_ratio if metrics else None),
        ),
        (
            "Max drawdown",
            _fmt_pct(metrics.max_drawdown if metrics else None),
        ),
        (
            "Gross turnover",
            _fmt_number(metrics.gross_turnover if metrics else None),
        ),
        (
            "Transaction-cost total",
            _fmt_number(metrics.transaction_cost_total if metrics else None),
        ),
        (
            "Successful rebalance count",
            _esc(metrics.successful_rebalance_count) if metrics is not None else "Unavailable",
        ),
    ]
    cards = "".join(
        f'<div class="metric-card"><div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div></div>'
        for label, value in rows
    )
    return f'<div class="metric-grid">{cards}</div>'


def _section_charts(result: BacktestResult, *, success: bool) -> str:
    equity = result.equity
    if not equity:
        return _section(
            "Charts",
            '<p class="data-unavailable">NAV and drawdown charts unavailable: '
            "no persisted equity points.</p>",
        )
    if not success and not any(math.isfinite(point.nav) for point in equity):
        return _section(
            "Charts",
            '<p class="data-unavailable">Charts unavailable for this non-successful '
            "run (no finite equity series).</p>",
        )
    note = ""
    points = list(equity)
    if len(points) > _MAX_CHART_POINTS:
        points = _downsample(points, _MAX_CHART_POINTS)
        note = (
            f'<p class="muted">Visualization-only sampling: showing {len(points)} of '
            f"{len(equity)} equity points. Full observations remain in JSON exports.</p>"
        )
    nav_svg = _nav_chart(points)
    dd_svg = _drawdown_chart(points)
    return _section(
        "Charts",
        note + "<h3>Portfolio NAV</h3>" + nav_svg + "<h3>Portfolio drawdown</h3>" + dd_svg,
    )


def _downsample(points: list[EquityPoint], limit: int) -> list[EquityPoint]:
    if len(points) <= limit:
        return points
    # Always keep first and last; sample evenly in between.
    if limit < 2:
        return [points[0]]
    out: list[EquityPoint] = [points[0]]
    inner = limit - 2
    for index in range(inner):
        pos = 1 + round(index * (len(points) - 2) / max(inner, 1))
        pos = min(max(pos, 1), len(points) - 2)
        out.append(points[pos])
    out.append(points[-1])
    # Deduplicate while preserving order.
    seen: set[int] = set()
    unique: list[EquityPoint] = []
    for point in out:
        key = id(point)
        if key in seen:
            continue
        seen.add(key)
        unique.append(point)
    return unique


def _nav_chart(points: list[EquityPoint]) -> str:
    finite = [p for p in points if math.isfinite(p.nav)]
    if not finite:
        return '<p class="data-unavailable">NAV chart data unavailable.</p>'
    width, height = 860, 320
    pad_l, pad_r, pad_t, pad_b = 56, 20, 24, 48
    xs = list(range(len(finite)))
    navs = [p.nav for p in finite]
    bench_pairs: list[tuple[int, float]] = []
    for index, point in enumerate(finite):
        if point.benchmark_nav is not None and math.isfinite(point.benchmark_nav):
            bench_pairs.append((index, point.benchmark_nav))
    y_vals = list(navs)
    y_vals.extend(value for _, value in bench_pairs)
    y_min = min(y_vals)
    y_max = max(y_vals)
    if y_min == y_max:
        y_min -= abs(y_min) * 0.01 + 1e-9
        y_max += abs(y_max) * 0.01 + 1e-9
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x_at(index: int) -> float:
        if len(finite) == 1:
            return pad_l + plot_w / 2.0
        return pad_l + plot_w * (index / (len(finite) - 1))

    def y_at(value: float) -> float:
        return pad_t + plot_h * (1.0 - (value - y_min) / (y_max - y_min))

    nav_poly = " ".join(f"{x_at(i):.2f},{y_at(v):.2f}" for i, v in enumerate(navs))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Portfolio and benchmark NAV">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fbfbfa"/>'
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height - pad_b}" '
        f'stroke="#c8c8c4" stroke-width="1"/>'
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" '
        f'y2="{height - pad_b}" stroke="#c8c8c4" stroke-width="1"/>'
        f'<polyline fill="none" stroke="#1f4e79" stroke-width="2" points="{nav_poly}"/>'
    ]
    if bench_pairs:
        # Draw only contiguous segments with actual stored benchmark values.
        segment: list[tuple[int, float]] = []
        for index, value in bench_pairs:
            if segment and index != segment[-1][0] + 1:
                parts.append(_bench_polyline(segment, x_at, y_at))
                segment = []
            segment.append((index, value))
        if segment:
            parts.append(_bench_polyline(segment, x_at, y_at))
        legend = (
            f'<text x="{pad_l}" y="16" font-size="12" fill="#1f4e79">'
            "Portfolio NAV</text>"
            f'<text x="{pad_l + 110}" y="16" font-size="12" fill="#6b7280">'
            "Benchmark NAV (stored values only)</text>"
        )
    else:
        legend = (
            f'<text x="{pad_l}" y="16" font-size="12" fill="#1f4e79">'
            "Portfolio NAV</text>"
            f'<text x="{pad_l + 110}" y="16" font-size="12" fill="#6b7280">'
            "Benchmark unavailable</text>"
        )
    parts.append(legend)
    parts.append(
        f'<text x="{pad_l}" y="{height - 12}" font-size="11" fill="#555">'
        f"{_esc(finite[0].valuation_at.date().isoformat())}</text>"
    )
    parts.append(
        f'<text x="{width - pad_r}" y="{height - 12}" font-size="11" fill="#555" '
        f'text-anchor="end">{_esc(finite[-1].valuation_at.date().isoformat())}</text>'
    )
    parts.append(
        f'<text x="8" y="{pad_t + 4}" font-size="11" fill="#555">{_esc(f"{y_max:.6g}")}</text>'
    )
    parts.append(
        f'<text x="8" y="{height - pad_b}" font-size="11" fill="#555">{_esc(f"{y_min:.6g}")}</text>'
    )
    parts.append("</svg>")
    # xs kept for potential future axis ticks; silence unused lint by referencing.
    _ = xs
    return "".join(parts)


def _bench_polyline(
    segment: list[tuple[int, float]],
    x_at: Any,
    y_at: Any,
) -> str:
    if len(segment) == 1:
        index, value = segment[0]
        return f'<circle cx="{x_at(index):.2f}" cy="{y_at(value):.2f}" r="2.5" fill="#6b7280"/>'
    points = " ".join(f"{x_at(i):.2f},{y_at(v):.2f}" for i, v in segment)
    return (
        f'<polyline fill="none" stroke="#6b7280" stroke-width="1.5" '
        f'stroke-dasharray="4 3" points="{points}"/>'
    )


def _drawdown_chart(points: list[EquityPoint]) -> str:
    finite = [p for p in points if math.isfinite(p.drawdown)]
    if not finite:
        return '<p class="data-unavailable">Drawdown chart data unavailable.</p>'
    width, height = 860, 280
    pad_l, pad_r, pad_t, pad_b = 56, 20, 24, 48
    values = [p.drawdown for p in finite]
    y_min = min(min(values), 0.0)
    y_max = max(max(values), 0.0)
    if y_min == y_max:
        y_min -= 0.01
        y_max += 0.01
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x_at(index: int) -> float:
        if len(finite) == 1:
            return pad_l + plot_w / 2.0
        return pad_l + plot_w * (index / (len(finite) - 1))

    def y_at(value: float) -> float:
        return pad_t + plot_h * (1.0 - (value - y_min) / (y_max - y_min))

    poly = " ".join(f"{x_at(i):.2f},{y_at(v):.2f}" for i, v in enumerate(values))
    zero_y = y_at(0.0)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Portfolio drawdown">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fbfbfa"/>'
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height - pad_b}" '
        f'stroke="#c8c8c4" stroke-width="1"/>'
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" '
        f'y2="{height - pad_b}" stroke="#c8c8c4" stroke-width="1"/>'
        f'<line x1="{pad_l}" y1="{zero_y:.2f}" x2="{width - pad_r}" '
        f'y2="{zero_y:.2f}" stroke="#d0d0cc" stroke-width="1" stroke-dasharray="3 3"/>'
        f'<polyline fill="none" stroke="#8b3a3a" stroke-width="2" points="{poly}"/>'
        f'<text x="{pad_l}" y="16" font-size="12" fill="#8b3a3a">'
        "Drawdown (signed)</text>"
        f'<text x="{pad_l}" y="{height - 12}" font-size="11" fill="#555">'
        f"{_esc(finite[0].valuation_at.date().isoformat())}</text>"
        f'<text x="{width - pad_r}" y="{height - 12}" font-size="11" fill="#555" '
        f'text-anchor="end">{_esc(finite[-1].valuation_at.date().isoformat())}</text>'
        f'<text x="8" y="{pad_t + 4}" font-size="11" fill="#555">'
        f"{_esc(f'{y_max * 100:.2f}%')}</text>"
        f'<text x="8" y="{height - pad_b}" font-size="11" fill="#555">'
        f"{_esc(f'{y_min * 100:.2f}%')}</text>"
        "</svg>"
    )


def _section_rebalances(result: BacktestResult) -> str:
    if not result.rebalances:
        return _section(
            "Rebalance history",
            '<p class="muted">No persisted rebalance records.</p>',
        )
    header = (
        "<tr>"
        "<th>decision_at</th><th>target_effective_at</th><th>status</th>"
        "<th>reason</th><th>pre-cost NAV</th><th>post-cost NAV</th>"
        "<th>gross turnover</th><th>cost amount</th>"
        "</tr>"
    )
    rows = []
    for row in result.rebalances:
        rows.append(
            "<tr>"
            f"<td>{_fmt_ts(row.decision_at)}</td>"
            f"<td>{_fmt_ts(row.target_effective_at)}</td>"
            f"<td>{_esc(row.status.value)}</td>"
            f"<td>{_esc(row.reason) if row.reason else '—'}</td>"
            f"<td>{_fmt_number(row.pre_cost_nav)}</td>"
            f"<td>{_fmt_number(row.post_cost_nav)}</td>"
            f"<td>{_fmt_number(row.gross_turnover)}</td>"
            f"<td>{_fmt_number(row.cost_amount)}</td>"
            "</tr>"
        )
    return _section(
        "Rebalance history",
        "<p>Calendar decision timestamps and target-effective sessions from the "
        "persisted weight-return research path.</p>"
        f'<div class="table-wrap"><table>{header}{"".join(rows)}</table></div>',
    )


def _section_transitions(result: BacktestResult) -> str:
    rows_html: list[str] = []
    for rebalance in result.rebalances:
        for row in rebalance.transitions:
            rows_html.append(
                "<tr>"
                f"<td>{_fmt_ts(rebalance.decision_at)}</td>"
                f"<td>{_esc(row.symbol)}</td>"
                f"<td>{_fmt_number(row.current_weight_before)}</td>"
                f"<td>{_fmt_number(row.target_weight_after)}</td>"
                f"<td>{_fmt_number(row.delta_weight)}</td>"
                f"<td>{_fmt_number(row.gross_notional)}</td>"
                f"<td>{_fmt_number(row.allocated_cost)}</td>"
                "</tr>"
            )
    if not rows_html:
        return _section(
            "Weight transitions",
            '<p class="muted">No persisted weight transitions. These rows are '
            "target-weight transitions with gross notional and transaction cost, "
            "not broker fills or executed shares.</p>",
        )
    header = (
        "<tr>"
        "<th>decision_at</th><th>symbol</th>"
        "<th>current weight before</th><th>target weight after</th>"
        "<th>delta</th><th>gross notional</th><th>allocated cost</th>"
        "</tr>"
    )
    return _section(
        "Weight transitions",
        "<p>Target weights and weight transitions from the research backtester. "
        "Gross notional and transaction cost are modeled; this is not an "
        "order-fill ledger.</p>"
        f'<div class="table-wrap"><table>{header}{"".join(rows_html)}</table></div>',
    )


def _section_signals(result: BacktestResult) -> str:
    rows_html: list[str] = []
    for rebalance in result.rebalances:
        for row in rebalance.transitions:
            if (
                row.signal_value is None
                and row.signal_rank is None
                and row.signal_period is None
                and not row.signal_provenance_json
            ):
                continue
            provenance = _format_provenance(row.signal_provenance_json)
            rows_html.append(
                "<tr>"
                f"<td>{_fmt_ts(rebalance.decision_at)}</td>"
                f"<td>{_esc(row.symbol)}</td>"
                f"<td>{_fmt_number(row.signal_value)}</td>"
                f"<td>{_esc(row.signal_rank) if row.signal_rank is not None else '—'}</td>"
                f"<td>{_esc(row.signal_period) if row.signal_period else '—'}</td>"
                f'<td class="prov">{provenance}</td>'
                "</tr>"
            )
    if not rows_html:
        return _section(
            "Signal evidence",
            '<p class="muted">No persisted signal values, ranks, periods, or '
            "provenance on weight-transition rows. Transition rows only record "
            "evidence for selected symbols that were stored with the run.</p>",
        )
    header = (
        "<tr>"
        "<th>decision_at</th><th>symbol</th><th>factor value</th>"
        "<th>signal rank</th><th>fiscal period</th><th>provenance</th>"
        "</tr>"
    )
    return _section(
        "Signal evidence",
        "<p>Evidence attached to persisted weight transitions for selected "
        "symbols. This is not a full candidate-universe ranking table.</p>"
        f'<div class="table-wrap"><table>{header}{"".join(rows_html)}</table></div>',
    )


def _format_provenance(raw: str | None) -> str:
    if not raw:
        return "Unavailable"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return f"<code>{_esc(raw)}</code>"
    if not isinstance(payload, dict):
        return f"<code>{_esc(raw)}</code>"
    preferred = (
        "ticker",
        "issuer_cik",
        "cik",
        "factor",
        "factor_name",
        "reporting_period",
        "period",
        "ranking_direction",
        "as_of",
        "available_at",
        "source_filings",
        "accessions",
        "accession",
        "inputs",
        "factor_inputs",
        "market_input",
        "market_inputs",
        "warnings",
    )
    lines: list[str] = []
    seen: set[str] = set()
    for key in preferred:
        if key in payload:
            lines.append(_provenance_line(key, payload[key]))
            seen.add(key)
    for key in sorted(payload.keys()):
        if key in seen:
            continue
        lines.append(_provenance_line(key, payload[key]))
    if not lines:
        return "Unavailable"
    return '<ul class="prov-list">' + "".join(lines) + "</ul>"


def _provenance_line(key: str, value: object) -> str:
    if value is None:
        rendered = "Unavailable"
    elif isinstance(value, (dict, list)):
        rendered = f"<code>{_esc(json.dumps(value, sort_keys=True, ensure_ascii=False))}</code>"
    else:
        rendered = f"<code>{_esc(value)}</code>"
    return f"<li><strong>{_esc(key)}</strong>: {rendered}</li>"


def _section_audit(result: BacktestResult) -> str:
    audit = result.audit
    if audit is None:
        audit_block = '<p class="muted">Audit unavailable for this run.</p>'
    else:
        failures = (
            "<ul>"
            + "".join(f"<li><code>{_esc(item)}</code></li>" for item in audit.failures)
            + "</ul>"
            if audit.failures
            else "<p>No audit failures recorded.</p>"
        )
        audit_warnings = (
            "<ul>"
            + "".join(f"<li><code>{_esc(item)}</code></li>" for item in audit.warnings)
            + "</ul>"
            if audit.warnings
            else ""
        )
        audit_block = (
            f"<p>Audit passed: <strong>{_esc(audit.passed)}</strong></p>"
            f"<h3>Audit failures</h3>{failures}"
            + (f"<h3>Audit warnings</h3>{audit_warnings}" if audit_warnings else "")
        )
    if result.warnings:
        warning_block = (
            "<h3>Stored run warnings</h3><ul>"
            + "".join(f"<li><code>{_esc(item)}</code></li>" for item in result.warnings)
            + "</ul>"
        )
    else:
        warning_block = "<h3>Stored run warnings</h3><p>None stored.</p>"
    failure = ""
    if result.failure_reason:
        failure = (
            "<h3>Unavailable / failure reason</h3>"
            f"<p><code>{_esc(result.failure_reason)}</code></p>"
        )
    return _section("Audit and run warnings", audit_block + warning_block + failure)


def _section_limitations(result: BacktestResult) -> str:
    general = (
        "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in _METHODOLOGY_LIMITATIONS) + "</ul>"
    )
    run_specific = ""
    if result.warnings:
        run_specific = (
            "<h3>Run-specific stored warnings</h3><ul>"
            + "".join(f"<li><code>{_esc(item)}</code></li>" for item in result.warnings)
            + "</ul>"
        )
    return _section(
        "Methodology limitations",
        "<h3>Research model limitations</h3>"
        + general
        + "<p>General methodology limitations above are distinct from "
        "run-specific audit failures and stored warnings.</p>" + run_specific,
    )


def _section(title: str, body: str) -> str:
    return f'<section class="block"><h2>{_esc(title)}</h2>{body}</section>'


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><th scope="row">{_esc(label)}</th><td>{value}</td></tr>' for label, value in rows
    )
    return f'<div class="table-wrap"><table class="kv">{body}</table></div>'


_CSS = """
:root {
  --bg: #eef1f4;
  --surface: #ffffff;
  --ink: #15202b;
  --muted: #4d5b69;
  --line: #cfd6de;
  --accent: #1f4e79;
  --ok: #1f6b3a;
  --warn: #8a5a00;
  --bad: #8b1e1e;
  --card: #f3f6f9;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Source Serif 4", "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  color: var(--ink);
  background:
    radial-gradient(1100px 480px at 8% -12%, #d9e4f0 0%, transparent 58%),
    radial-gradient(900px 420px at 100% 0%, #dfe7ee 0%, transparent 52%),
    var(--bg);
  line-height: 1.45;
}
.page {
  max-width: 980px;
  margin: 0 auto;
  padding: 2rem 1.25rem 4rem;
}
.hero h1 {
  margin: 0;
  font-size: clamp(2.2rem, 5vw, 3.2rem);
  letter-spacing: -0.02em;
  color: var(--accent);
}
.subtitle {
  margin: 0.35rem 0 1.5rem;
  color: var(--muted);
  font-size: 1.05rem;
}
.block {
  background: var(--surface);
  border: 1px solid var(--line);
  padding: 1.25rem 1.35rem;
  margin: 0 0 1.1rem;
}
.block h2 {
  margin: 0 0 0.75rem;
  font-size: 1.25rem;
  border-bottom: 1px solid var(--line);
  padding-bottom: 0.4rem;
}
.block h3 {
  margin: 1rem 0 0.5rem;
  font-size: 1.02rem;
}
.banner {
  border: 1px solid var(--line);
  padding: 1rem 1.25rem;
  margin-bottom: 1.1rem;
}
.banner h2 { margin: 0 0 0.4rem; border: 0; padding: 0; }
.banner-success { background: #edf7f0; border-color: #b7d7c1; color: var(--ok); }
.banner-unavailable { background: #fff6e8; border-color: #e2c48a; color: var(--warn); }
.banner-failed { background: #fbecec; border-color: #e0a8a8; color: var(--bad); }
.banner-other { background: #f0f0ec; }
.banner-reason, .banner-note { margin: 0.35rem 0 0; color: var(--ink); }
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 0.75rem;
}
.metric-card {
  background: var(--card);
  border: 1px solid var(--line);
  padding: 0.75rem 0.85rem;
}
.metric-label {
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
}
.metric-value {
  margin-top: 0.25rem;
  font-size: 1.15rem;
  font-variant-numeric: tabular-nums;
}
.table-wrap { overflow-x: auto; }
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.92rem;
  font-variant-numeric: tabular-nums;
}
th, td {
  border-bottom: 1px solid var(--line);
  padding: 0.45rem 0.5rem;
  text-align: left;
  vertical-align: top;
}
table.kv th {
  width: 32%;
  color: var(--muted);
  font-weight: 600;
}
code {
  font-family: ui-monospace, "Cascadia Code", "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.86em;
  word-break: break-word;
}
.muted { color: var(--muted); }
.data-unavailable {
  color: var(--warn);
  background: #fff8ec;
  border: 1px solid #ecd7a8;
  padding: 0.75rem 0.9rem;
}
.prov-list { margin: 0; padding-left: 1.1rem; }
.prov { min-width: 18rem; }
svg {
  width: 100%;
  height: auto;
  display: block;
  margin: 0.5rem 0 1rem;
  border: 1px solid var(--line);
}
@media print {
  body { background: #fff; }
  .page { max-width: none; padding: 0; }
  .block, .banner { break-inside: avoid; }
}
@media (max-width: 640px) {
  .page { padding: 1.25rem 0.85rem 3rem; }
  table.kv th { width: 40%; }
}
"""
