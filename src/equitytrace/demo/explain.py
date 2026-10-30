"""Plain-language explanations for engine reason codes and metrics.

Texts only restate behaviour the engine already implements; they never change a
result. An unknown code is shown verbatim with a note rather than guessed at.
"""

from __future__ import annotations

from equitytrace.portfolio.models import REQUIRED_RETURNS

REASONS: dict[str, str] = {
    "factor_universe_too_small": (
        "Fewer than top-N tickers had a valid factor value from filings that were public at "
        "the first decision time. The engine uses only each issuer's newest public annual "
        "report and never falls back to an older year."
    ),
    "issuer_identity_unavailable": (
        "At least one ticker is not mapped to a known issuer in the stored data."
    ),
    "multiple_securities_same_issuer_unsupported": (
        "Two or more universe tickers belong to the same issuer (for example two share "
        "classes). Fundamentals are issuer-level, so selecting both would double count one "
        "company; the engine refuses instead of choosing a class."
    ),
    "calendar_missing": (
        "The calendar instrument has no stored sessions for the requested window, or a "
        "requested rebalance date is not a stored session."
    ),
    "baseline_unavailable": (
        f"The weighting method could not be formed. Inverse-volatility and minimum-variance "
        f"need {REQUIRED_RETURNS + 1} common daily closes (about {REQUIRED_RETURNS} returns) "
        "for every selected name before the decision time."
    ),
    "target_support_missing": (
        "A selected security has no price bar on the target-effective session."
    ),
    "held_return_missing": (
        "A held security has no usable return for a session. The engine fails rather than "
        "assuming a zero return."
    ),
    "leakage_detected": (
        "The point-in-time audit found evidence that was not public at the decision time."
    ),
    "database_error": "The database reported an error while running or saving the research.",
    "explicit_date_out_of_range": "An explicit rebalance date falls outside the date range.",
    "effective_session_out_of_range": (
        "The session after an explicit decision date falls after the end date."
    ),
    "optimizer_fit_failed": "The minimum-variance optimizer did not produce weights.",
    "optimizer_input_invalid": "The optimizer received invalid return data.",
    "optimizer_invalid_weights": "The optimizer returned invalid weights.",
    "optimizer_budget_violation": "The optimizer weights did not sum to one.",
}

METRIC_HELP: dict[str, str] = {
    "total_return": "Final NAV divided by initial NAV, minus one.",
    "annualized_return": "Total growth compounded to 252 sessions per year.",
    "annualized_volatility": "Standard deviation of daily NAV returns times √252.",
    "sharpe_ratio": "Mean daily return over its standard deviation times √252, risk-free = 0.",
    "max_drawdown": "Largest peak-to-trough NAV decline (negative number).",
    "gross_turnover": (
        "Sum over rebalances of Σ|target weight - drifted weight|. Weight changes, not fills."
    ),
    "transaction_cost_total": (
        "Costs charged only on actual weight transitions: NAV x turnover x cost bps."
    ),
}

WARNING_TEXT: dict[str, str] = {
    "provider_adjusted_history_not_vintage_pit": (
        "Adjusted prices are a retrospective series, not a historical vendor-vintage archive."
    ),
    "survivorship_bias_possible": (
        "The universe is chosen by the user today and may exclude issuers that disappeared."
    ),
    "mixed_fiscal_periods": (
        "Selected names were ranked on different fiscal years because their 10-Ks were filed "
        "on different dates."
    ),
    "top_n_tie_at_cutoff": (
        "Several names tied at the top-N cutoff; ticker ascending (non-economic) broke the tie."
    ),
    "benchmark_unavailable": "The benchmark lacked a usable return and was dropped from the run.",
}

GENERIC_LIMITATIONS: tuple[str, ...] = (
    "Synthetic research evidence: every issuer, filing and price in this demo is fictional.",
    "Hypothetical portfolio simulation: weights drift with returns; no orders or fills exist.",
    "No live trading and no investment advice.",
    "Historical universe/survivorship limitation: the universe is a fixed list of tickers.",
    "Prices are a non-vintage adjusted series, not a point-in-time vendor archive.",
    "The demo does not certify the live SEC ingestion capacity or crash-recovery gates.",
)


def explain_reason(code: str | None) -> str:
    if not code:
        return "No reason recorded."
    text = REASONS.get(code)
    if text is None:
        return f"{code} (no further explanation is registered for this reason code)."
    return text
