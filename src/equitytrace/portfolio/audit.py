"""Point-in-time leakage checks for research backtests."""

from __future__ import annotations

from datetime import date, datetime

from equitytrace.portfolio.models import (
    ADJUSTED_HISTORY_WARNING,
    MIXED_PERIODS_WARNING,
    SURVIVORSHIP_WARNING,
    LeakageAuditResult,
    SignalSelection,
)


def audit_decision(
    *,
    decision_at: datetime,
    decision_session_date: date,
    target_effective_at: datetime,
    selection: SignalSelection,
    mixed_periods: bool,
) -> list[str]:
    """Return hard leakage failures for one decision. Empty means the decision is clean."""
    del mixed_periods
    failures: list[str] = []
    if not (decision_at < target_effective_at):
        failures.append("decision_at must be strictly before target_effective_at")
    for ticker, known in selection.evidence_available_at.items():
        if known > decision_at:
            failures.append(f"factor evidence for {ticker} available_at is after decision_at")
    for known in selection.price_evidence_available_at:
        if known > decision_at:
            failures.append("price evidence available_at is after decision_at")
    for evidence_date in selection.price_evidence_dates:
        if evidence_date > decision_session_date:
            failures.append("price evidence trading date is after decision session")
    return failures


def build_audit_result(
    *,
    failures: list[str],
    mixed_periods: bool,
    extra_warnings: tuple[str, ...] = (),
) -> LeakageAuditResult:
    warnings = list(
        dict.fromkeys((ADJUSTED_HISTORY_WARNING, SURVIVORSHIP_WARNING, *extra_warnings))
    )
    if mixed_periods and MIXED_PERIODS_WARNING not in warnings:
        warnings.append(MIXED_PERIODS_WARNING)
    unique_failures = tuple(dict.fromkeys(failures))
    return LeakageAuditResult(
        passed=not unique_failures,
        failures=unique_failures,
        warnings=tuple(warnings),
    )
