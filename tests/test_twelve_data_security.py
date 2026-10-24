"""Twelve Data API-key leakage regressions (issue #20)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from typer.testing import CliRunner

from equitytrace.cli import _configure_logging, app
from equitytrace.config import Settings, clear_settings_cache
from equitytrace.market.models import PriceAdjustmentMode
from equitytrace.market.providers.errors import (
    MarketDataAuthError,
    MarketDataTransientError,
)
from equitytrace.market.providers.twelve_data import TwelveDataProvider

runner = CliRunner()

# Synthetic only — never a real environment key.
_SYNTHETIC_KEY = "synth-test-key-DO-NOT-USE-IN-PROD-9f3a"


def _settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = _SYNTHETIC_KEY,
    enable_cache: bool = False,
    retries: int = 3,
) -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", key)
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "sec.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_CACHE_DIR", str(tmp_path / "mcache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true" if enable_cache else "false")
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_MAX_RETRIES", str(retries))
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_REQUESTS_PER_MINUTE", "1000")
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _payload() -> dict[str, object]:
    return {
        "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
        "status": "ok",
        "values": [
            {
                "datetime": "2023-01-03",
                "open": "10",
                "high": "10",
                "low": "10",
                "close": "10",
                "volume": "100",
            }
        ],
    }


def test_authorization_header_and_no_query_apikey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload())

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )

    assert seen
    request = seen[0]
    query = parse_qs(urlparse(str(request.url)).query)
    assert "apikey" not in query
    assert _SYNTHETIC_KEY not in str(request.url)
    assert request.headers.get("Authorization") == f"apikey {_SYNTHETIC_KEY}"
    assert len(result.bars) == 1
    assert str(result.bars[0].close) == "10"


def test_cached_response_still_works_without_key_in_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, enable_cache=True)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert "apikey" not in str(request.url)
        assert request.headers.get("Authorization") == f"apikey {_SYNTHETIC_KEY}"
        return httpx.Response(200, json=_payload())

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        first = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
        second = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
    assert calls["n"] == 1
    assert first.bars == second.bars
    cache_files = list(settings.market_data_cache_dir.glob("td_*.json"))
    assert len(cache_files) == 1
    text = cache_files[0].read_text(encoding="utf-8")
    assert _SYNTHETIC_KEY not in text
    assert "apikey" not in text


def test_debug_caplog_contains_no_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _configure_logging(verbose=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload())

    with (
        caplog.at_level(logging.DEBUG),
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert _SYNTHETIC_KEY not in joined
    assert f"apikey={_SYNTHETIC_KEY}" not in joined
    # Transport loggers must stay at WARNING even under --verbose.
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_cli_logging_default_and_verbose_hide_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    for verbose in (False, True):
        _configure_logging(verbose=verbose)
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING

        def handler(request: httpx.Request) -> httpx.Response:
            # Mimic httpx's INFO request URL log; our clamp should keep it out
            # of CLI-configured output paths, and the URL itself has no key.
            logging.getLogger("httpx").info("HTTP Request: GET %s", request.url)
            return httpx.Response(200, json=_payload())

        with (
            caplog.at_level(logging.DEBUG),
            TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        ):
            provider.fetch_daily_bars(
                "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert _SYNTHETIC_KEY not in joined
        caplog.clear()


def test_auth_failure_message_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    payload = {
        "status": "error",
        "code": 401,
        "message": f"Invalid API key {_SYNTHETIC_KEY}",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        pytest.raises(MarketDataAuthError) as exc_info,
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    assert _SYNTHETIC_KEY not in str(exc_info.value)
    assert _SYNTHETIC_KEY not in repr(exc_info.value)


def test_transport_failure_message_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, retries=1)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for key={_SYNTHETIC_KEY}")

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        pytest.raises(MarketDataTransientError) as exc_info,
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    assert _SYNTHETIC_KEY not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_retry_path_messages_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch, retries=3)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(
                200,
                json={
                    "status": "error",
                    "code": 500,
                    "message": f"upstream {_SYNTHETIC_KEY}",
                },
            )
        return httpx.Response(200, json=_payload())

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
    assert attempts["n"] == 3


def test_http_401_has_no_key_in_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"denied {_SYNTHETIC_KEY}")

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        pytest.raises(MarketDataAuthError) as exc_info,
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    assert _SYNTHETIC_KEY not in str(exc_info.value)


def test_cli_help_documents_refresh() -> None:
    import re

    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    # Rich may style "--refresh" as separate ANSI spans; strip codes first.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--refresh" in plain
