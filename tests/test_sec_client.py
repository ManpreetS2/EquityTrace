"""Tests for the SEC HTTP client."""

from __future__ import annotations

import httpx
import pytest

from equitytrace.config import ConfigurationError, Settings, clear_settings_cache
from equitytrace.sec.client import COMPANY_TICKERS_URL, RetryableSecError, SecClient, SecHttpError


def test_missing_sec_email_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("EQUITYTRACE_SEC_EMAIL", "FILINGEDGE_SEC_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "")
    clear_settings_cache()
    settings = Settings()
    with pytest.raises(ConfigurationError, match="EQUITYTRACE_SEC_EMAIL"):
        settings.user_agent()


def test_user_agent_contains_org_and_email(settings: Settings) -> None:
    ua = settings.user_agent()
    assert "EquityTrace Tests" in ua
    assert "EquityTrace/" in ua
    assert "tests@example.com" in ua


def test_request_headers_include_user_agent(
    settings: Settings,
    sec_mock_transport: httpx.MockTransport,
) -> None:
    captured: list[httpx.Request] = []

    def wrapping(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return sec_mock_transport.handle_request(request)

    with SecClient(settings, transport=httpx.MockTransport(wrapping)) as client:
        payload = client.get_company_tickers()
    assert payload
    assert captured
    assert captured[0].headers["User-Agent"] == settings.user_agent()
    assert "Accept" in captured[0].headers


def test_retry_on_server_error(settings: Settings) -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    with SecClient(settings, transport=httpx.MockTransport(handler)) as client:
        payload = client.get_json("https://data.sec.gov/example.json", use_cache=False)
    assert payload == {"ok": True}
    assert attempts["count"] == 3


def test_retry_on_429_is_bounded(settings: Settings) -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(429, text="rate limited")

    with (
        SecClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RetryableSecError, match="429"),
    ):
        client.get_json("https://data.sec.gov/example.json", use_cache=False)
    # stop_after_attempt(4) => exactly four tries, then raise.
    assert attempts["count"] == 4


def test_non_retryable_client_error(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    with (
        SecClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SecHttpError, match="404") as exc_info,
    ):
        client.get_json(COMPANY_TICKERS_URL, use_cache=False)
    assert not isinstance(exc_info.value, RetryableSecError)
    assert exc_info.value.status_code == 404


def test_timeout_is_retryable(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    with (
        SecClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RetryableSecError, match="Timeout"),
    ):
        client.get_json(COMPANY_TICKERS_URL, use_cache=False)
