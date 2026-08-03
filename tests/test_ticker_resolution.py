"""Tests for ticker-to-CIK resolution."""

from __future__ import annotations

import pytest

from filingedge.models import normalize_cik
from filingedge.sec.client import SecClient, SecClientError
from filingedge.sec.tickers import resolve_ticker


def test_normalize_cik_zero_padding() -> None:
    assert normalize_cik(320193) == "0000320193"
    assert normalize_cik("320193") == "0000320193"
    assert normalize_cik("0000320193") == "0000320193"


def test_normalize_cik_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="Invalid CIK"):
        normalize_cik("not-a-cik")


def test_resolve_ticker_aapl(sec_client: SecClient) -> None:
    resolved = resolve_ticker(sec_client, "aapl")
    assert resolved.ticker == "AAPL"
    assert resolved.cik == "0000320193"
    assert resolved.company_name == "Apple Inc."


def test_resolve_ticker_not_found(sec_client: SecClient) -> None:
    with pytest.raises(SecClientError, match="Ticker not found"):
        resolve_ticker(sec_client, "ZZZZZ")


def test_resolve_ticker_empty(sec_client: SecClient) -> None:
    with pytest.raises(SecClientError, match="empty"):
        resolve_ticker(sec_client, "   ")
