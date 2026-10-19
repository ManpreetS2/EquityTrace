"""Tests for the SEC HTTP client."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from equitytrace.config import ConfigurationError, Settings, clear_settings_cache
from equitytrace.sec.client import (
    COMPANY_FACTS_URL,
    COMPANY_TICKERS_URL,
    SUBMISSIONS_ARCHIVE_BASE,
    SUBMISSIONS_URL,
    RetryableSecError,
    SecClient,
    SecHttpError,
)


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


def test_mutable_cache_hit_within_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_SEC_CACHE_TTL_SECONDS", "3600")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "n": calls["n"]})

    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    with SecClient(settings, transport=httpx.MockTransport(handler)) as client:
        first = client.get_json(url)
        second = client.get_json(url)
    assert first == {"ok": True, "n": 1}
    assert second == {"ok": True, "n": 1}
    assert calls["n"] == 1


def test_expired_cache_triggers_refetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_SEC_CACHE_TTL_SECONDS", "60")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "n": calls["n"]})

    url = SUBMISSIONS_URL.format(cik="0000320193")
    with SecClient(settings, transport=httpx.MockTransport(handler)) as client:
        first = client.get_json(url)
        cache_path = next(settings.cache_dir.glob("*.json"))
        # Backdate mtime beyond TTL.
        import os
        import time

        past = time.time() - 120
        os.utime(cache_path, (past, past))
        second = client.get_json(url)
        assert client.cache_stats.expired_skips >= 1
    assert first == {"ok": True, "n": 1}
    assert second == {"ok": True, "n": 2}
    assert calls["n"] == 2


def test_force_refresh_bypasses_valid_cache_and_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_SEC_CACHE_TTL_SECONDS", "86400")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "n": calls["n"]})

    url = COMPANY_TICKERS_URL
    transport = httpx.MockTransport(handler)
    with SecClient(settings, transport=transport) as client:
        first = client.get_json(url)
    with SecClient(settings, transport=transport, force_refresh=True) as client:
        refreshed = client.get_json(url)
        assert client.cache_stats.forced_refreshes >= 1
        assert client.cache_stats.network_fetches == 1
    with SecClient(settings, transport=transport) as client:
        cached = client.get_json(url)
    assert first == {"ok": True, "n": 1}
    assert refreshed == {"ok": True, "n": 2}
    assert cached == {"ok": True, "n": 2}  # refresh replaced cache
    assert calls["n"] == 2


def test_failed_refresh_does_not_return_expired_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_SEC_CACHE_TTL_SECONDS", "30")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    state = {"mode": "ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["mode"] == "ok":
            return httpx.Response(200, json={"ok": True, "version": 1})
        return httpx.Response(503, text="unavailable")

    url = COMPANY_FACTS_URL.format(cik="0000320193")
    transport = httpx.MockTransport(handler)
    with SecClient(settings, transport=transport) as client:
        assert client.get_json(url) == {"ok": True, "version": 1}
        cache_path = next(settings.cache_dir.glob("*.json"))
        import os
        import time

        past = time.time() - 120
        os.utime(cache_path, (past, past))

    state["mode"] = "fail"
    with (
        SecClient(settings, transport=transport) as client,
        pytest.raises(RetryableSecError),
    ):
        client.get_json(url)


def test_corrupt_cache_refetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "n": calls["n"]})

    url = "https://data.sec.gov/example-corrupt.json"
    with SecClient(settings, transport=httpx.MockTransport(handler)) as client:
        client.get_json(url)
        cache_path = next(settings.cache_dir.glob("*.json"))
        cache_path.write_text("{not-json", encoding="utf-8")
        recovered = client.get_json(url)
    assert recovered == {"ok": True, "n": 2}
    assert calls["n"] == 2


def test_archive_cache_is_immutable_by_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_SEC_CACHE_TTL_SECONDS", "1")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"archive": True, "n": calls["n"]})

    url = f"{SUBMISSIONS_ARCHIVE_BASE}CIK0000320193-submissions-001.json"
    with SecClient(settings, transport=httpx.MockTransport(handler)) as client:
        first = client.get_json(url)
        cache_path = next(settings.cache_dir.glob("*.json"))
        import os
        import time

        past = time.time() - 10_000
        os.utime(cache_path, (past, past))
        second = client.get_json(url)
    assert first == second == {"archive": True, "n": 1}
    assert calls["n"] == 1


def test_company_tickers_submissions_facts_obey_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_SEC_CACHE_TTL_SECONDS", "45")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls[url] = calls.get(url, 0) + 1
        return httpx.Response(200, json={"url": url, "n": calls[url]})

    urls = [
        COMPANY_TICKERS_URL,
        SUBMISSIONS_URL.format(cik="0000320193"),
        COMPANY_FACTS_URL.format(cik="0000320193"),
    ]
    transport = httpx.MockTransport(handler)
    with SecClient(settings, transport=transport) as client:
        for url in urls:
            client.get_json(url)
        import os
        import time

        past = time.time() - 120
        for path in settings.cache_dir.glob("*.json"):
            os.utime(path, (past, past))
        for url in urls:
            client.get_json(url)
    assert all(calls[url] == 2 for url in urls)


def test_no_unbounded_requests_on_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    url = COMPANY_TICKERS_URL
    with SecClient(settings, transport=httpx.MockTransport(handler)) as client:
        for _ in range(5):
            client.get_json(url)
    assert calls["n"] == 1
