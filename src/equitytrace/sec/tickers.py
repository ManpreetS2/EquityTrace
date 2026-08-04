"""Ticker-to-CIK resolution using the SEC company tickers file."""

from __future__ import annotations

from typing import Any

from equitytrace.models import ResolvedTicker, normalize_cik
from equitytrace.sec.client import SecClient, SecClientError


def resolve_ticker(client: SecClient, ticker: str) -> ResolvedTicker:
    """Resolve a stock ticker to an SEC CIK and company name."""
    normalized = ticker.strip().upper()
    if not normalized:
        raise SecClientError("Ticker must not be empty")

    payload = client.get_company_tickers()
    match = _find_ticker_entry(payload, normalized)
    if match is None:
        raise SecClientError(f"Ticker not found in SEC company tickers: {normalized}")

    cik = normalize_cik(match["cik_str"])
    company_name = str(match.get("title") or "").strip() or normalized
    exchange = _optional_str(match.get("exchange"))
    return ResolvedTicker(
        ticker=normalized,
        cik=cik,
        company_name=company_name,
        exchange=exchange,
    )


def _find_ticker_entry(payload: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        entry_ticker = str(entry.get("ticker") or "").strip().upper()
        if entry_ticker == ticker:
            return entry
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
