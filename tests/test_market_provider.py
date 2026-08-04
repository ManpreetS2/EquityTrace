"""Offline Twelve Data provider tests (fictional fixtures)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from equitytrace.config import Settings, clear_settings_cache
from equitytrace.market.models import PriceAdjustmentMode
from equitytrace.market.providers.errors import (
    MarketDataAuthError,
    MarketDataConfigError,
    MarketDataEmptyError,
    MarketDataError,
    MarketDataSymbolError,
    MarketDataTransientError,
)
from equitytrace.market.providers.twelve_data import (
    TwelveDataProvider,
    _date_windows,
    validate_ohlcv,
)


def _settings(tmp_path, monkeypatch: pytest.MonkeyPatch, *, key: str = "test-key") -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", key)
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_CACHE_DIR", str(tmp_path / "mcache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_MAX_RETRIES", "3")
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _ok_payload(*, descending: bool = False) -> dict[object, object]:
    values = [
        {
            "datetime": "2023-01-03",
            "open": "100.0",
            "high": "110.0",
            "low": "95.0",
            "close": "105.0",
            "volume": "1000",
        },
        {
            "datetime": "2023-01-04",
            "open": "105.0",
            "high": "112.0",
            "low": "104.0",
            "close": "111.0",
            "volume": "1200",
        },
    ]
    if descending:
        values = list(reversed(values))
    return {
        "meta": {
            "symbol": "AAPL",
            "currency": "USD",
            "exchange_timezone": "America/New_York",
        },
        "values": values,
        "status": "ok",
    }


def test_missing_api_key_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.delenv("EQUITYTRACE_TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(MarketDataConfigError, match="EQUITYTRACE_TWELVE_DATA_API_KEY"):
        TwelveDataProvider(settings)


def test_successful_raw_and_adjusted_ascending(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_ok_payload()))
    with TwelveDataProvider(settings, transport=transport) as provider:
        raw = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.NONE
        )
        adj = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.ALL
        )
    assert [b.trading_date for b in raw.bars] == [date(2023, 1, 3), date(2023, 1, 4)]
    assert raw.bars[0].close == Decimal("105.0")
    assert adj.adjustment_mode is PriceAdjustmentMode.ALL


def test_descending_normalized(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_ok_payload(descending=True))
    )
    with TwelveDataProvider(settings, transport=transport) as provider:
        raw = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.NONE
        )
    assert [b.trading_date for b in raw.bars] == [date(2023, 1, 3), date(2023, 1, 4)]


def test_duplicate_dates_keep_last(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _ok_payload()
    payload["values"] = [
        {
            "datetime": "2023-01-03",
            "open": "1",
            "high": "2",
            "low": "1",
            "close": "1.5",
            "volume": "10",
        },
        {
            "datetime": "2023-01-03",
            "open": "10",
            "high": "20",
            "low": "10",
            "close": "15",
            "volume": "99",
        },
    ]
    settings = _settings(tmp_path, monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with TwelveDataProvider(settings, transport=transport) as provider:
        raw = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.NONE
        )
    assert len(raw.bars) == 1
    assert raw.bars[0].close == Decimal("15")


def test_invalid_api_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="denied"))
    with (
        TwelveDataProvider(settings, transport=transport) as provider,
        pytest.raises(MarketDataAuthError),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )


def test_invalid_symbol_provider_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    payload = {"status": "error", "code": 400, "message": "Invalid symbol"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with (
        TwelveDataProvider(settings, transport=transport) as provider,
        pytest.raises(MarketDataSymbolError),
    ):
        provider.fetch_daily_bars(
            "ZZZZZ", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )


def test_http_429_retries(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=_ok_payload())

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        raw = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.NONE
        )
    assert attempts["n"] == 3
    assert raw.bars


def test_http_500_retries_then_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        pytest.raises(MarketDataTransientError),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )


def test_provider_error_in_http_200(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    payload = {"status": "error", "code": 500, "message": "upstream"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with (
        TwelveDataProvider(settings, transport=transport) as provider,
        pytest.raises(MarketDataError),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )


def test_malformed_numeric_row_skipped(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _ok_payload()
    payload["values"] = [
        {
            "datetime": "2023-01-03",
            "open": "bad",
            "high": "2",
            "low": "1",
            "close": "1.5",
            "volume": "10",
        },
        {
            "datetime": "2023-01-04",
            "open": "10",
            "high": "20",
            "low": "10",
            "close": "15",
            "volume": "99",
        },
    ]
    settings = _settings(tmp_path, monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with TwelveDataProvider(settings, transport=transport) as provider:
        raw = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.NONE
        )
    assert len(raw.bars) == 1
    assert raw.bars[0].trading_date == date(2023, 1, 4)


def test_missing_ohlc_field_skipped(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
        "values": [{"datetime": "2023-01-03", "open": "1", "high": "2", "close": "1.5"}],
        "status": "ok",
    }
    settings = _settings(tmp_path, monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with (
        TwelveDataProvider(settings, transport=transport) as provider,
        pytest.raises(MarketDataEmptyError),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.NONE
        )


def test_api_key_absent_from_errors(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "super-secret-twelve-data-key"
    settings = _settings(tmp_path, monkeypatch, key=secret)
    payload = {
        "status": "error",
        "code": 401,
        "message": f"bad key {secret}",
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with (
        TwelveDataProvider(settings, transport=transport) as provider,
        pytest.raises(MarketDataAuthError) as exc,
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    assert secret not in str(exc.value)


def test_date_windows_and_overlap_dedup() -> None:
    windows = _date_windows(date(2020, 1, 1), date(2022, 1, 1), 400)
    assert windows[0][0] == date(2020, 1, 1)
    assert windows[-1][1] == date(2022, 1, 1)
    assert len(windows) >= 2


def test_validate_ohlcv() -> None:
    assert (
        validate_ohlcv(
            open_=Decimal("10"),
            high=Decimal("12"),
            low=Decimal("9"),
            close=Decimal("11"),
            volume=1,
        )
        == []
    )
    assert "high_below_low" in validate_ohlcv(
        open_=Decimal("10"),
        high=Decimal("8"),
        low=Decimal("9"),
        close=Decimal("11"),
        volume=1,
    )
