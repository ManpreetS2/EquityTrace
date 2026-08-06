"""Release-readiness regression tests for EquityTrace v0.3a audit fixes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from equitytrace.config import Settings, clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.market.models import (
    DailyBarsResponse,
    MarketDataProviderName,
    MarketDataRunStatus,
    PriceAdjustmentMode,
    ProviderBar,
)
from equitytrace.market.service import MarketDataService
from equitytrace.sec.client import SecClient, SecClientError, sanitize_archive_filename


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_SEC_ORGANIZATION", "EquityTrace Tests")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "audit.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "sec-cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_CACHE_DIR", str(tmp_path / "mcache"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


class _FakeProvider:
    name = MarketDataProviderName.TWELVE_DATA.value

    def __init__(self, response: DailyBarsResponse) -> None:
        self.response = response
        self.closed = 0

    def fetch_daily_bars(
        self,
        provider_symbol: str,
        start_date: date,
        end_date: date,
        adjustment_mode: PriceAdjustmentMode,
    ) -> DailyBarsResponse:
        del provider_symbol, start_date, end_date, adjustment_mode
        return self.response

    def close(self) -> None:
        self.closed += 1


def _bar(day: str, *, tz: str = "America/New_York", currency: str = "USD") -> ProviderBar:
    return ProviderBar(
        trading_date=date.fromisoformat(day),
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=100,
        currency=currency,
        exchange_timezone=tz,
    )


def test_database_path_with_spaces_and_nested_dirs(tmp_path: Path) -> None:
    spaced = tmp_path / "research data" / "local db" / "equity trace.duckdb"
    db = initialize_database(spaced)
    assert spaced.exists()
    initialize_database(spaced)
    with db.session(read_only=True) as conn:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        versions = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
    assert {
        "financial_facts",
        "factor_values",
        "market_instruments",
        "daily_price_bars",
        "schema_migrations",
    }.issubset(tables)
    assert "0.2.0" in versions
    assert "0.3.0-a" in versions


def test_service_rejects_bars_outside_requested_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider(
        DailyBarsResponse(
            provider=MarketDataProviderName.TWELVE_DATA,
            provider_symbol="OUT",
            adjustment_mode=PriceAdjustmentMode.NONE,
            currency="USD",
            exchange_timezone="America/New_York",
            bars=(
                _bar("2019-01-02"),
                _bar("2023-01-03"),
                _bar("2024-06-01"),
            ),
        )
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "OUT",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 31),
            modes=[PriceAdjustmentMode.NONE],
        )
        dates = [
            row[0]
            for row in conn.execute(
                "SELECT trading_date FROM daily_price_bars ORDER BY trading_date"
            ).fetchall()
        ]
    assert result.status is MarketDataRunStatus.PARTIAL
    assert result.inserted_row_count == 1
    assert result.rejected_row_count == 2
    assert dates == [date(2023, 1, 3)]
    assert fake.closed == 0  # injected providers remain caller-owned


def test_invalid_timezone_not_confirmed_on_instrument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider(
        DailyBarsResponse(
            provider=MarketDataProviderName.TWELVE_DATA,
            provider_symbol="BADTZ",
            adjustment_mode=PriceAdjustmentMode.NONE,
            currency="USD",
            exchange_timezone="Not/A_Real_Zone",
            bars=(_bar("2023-01-03", tz="Not/A_Real_Zone"),),
        )
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "BADTZ",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 31),
            modes=[PriceAdjustmentMode.NONE],
        )
        row = conn.execute(
            """
            SELECT currency, exchange_timezone, market_metadata_confirmed
            FROM market_instruments
            """
        ).fetchone()
        bar_count = conn.execute("SELECT COUNT(*) FROM daily_price_bars").fetchone()
    assert result.status is MarketDataRunStatus.FAILED
    assert row is not None
    assert row[0] == "USD"
    assert row[1] == "America/New_York"
    assert row[2] is False
    assert bar_count is not None and int(bar_count[0]) == 0


def test_sec_cache_write_is_atomic_and_corrupt_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "n": calls["n"]})

    url = "https://data.sec.gov/example-cache.json"
    with SecClient(settings, transport=httpx.MockTransport(handler)) as client:
        first = client.get_json(url)
        second = client.get_json(url)
        cache_files = list(settings.cache_dir.glob("*.json"))
        assert len(cache_files) == 1
        cache_path = cache_files[0]
        assert not cache_path.with_suffix(".tmp").exists()
        cache_path.write_text("{not-json", encoding="utf-8")
        recovered = client.get_json(url)

    assert first == {"ok": True, "n": 1}
    assert second == {"ok": True, "n": 1}  # cache hit
    assert recovered == {"ok": True, "n": 2}  # corrupt ignored, refetch
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "filename",
    [
        "../evil.json",
        "submissions/../evil.json",
        "CIK0000320193-submissions-001.json/../../x.json",
        "evil.json.txt",
        "has space.json",
        "",
        "no-extension",
    ],
)
def test_archive_filename_sanitization_rejects_unsafe(filename: str) -> None:
    with pytest.raises(SecClientError, match=r"Refusing|must not be empty"):
        sanitize_archive_filename(filename)


def test_archive_filename_sanitization_accepts_sec_style() -> None:
    name = sanitize_archive_filename("CIK0000320193-submissions-001.json")
    assert name == "CIK0000320193-submissions-001.json"


def test_get_archived_submissions_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, EQUITYTRACE_ENABLE_CACHE="false")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"accessionNumber": []})

    with (
        SecClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SecClientError, match="Refusing"),
    ):
        client.get_archived_submissions("../evil.json")
    assert seen == []
