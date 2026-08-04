"""Adversarial audit tests for EquityTrace v0.3a market-data foundation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import duckdb
import httpx
import pytest
from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import Settings, clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.market.availability import bar_available_at
from equitytrace.market.market_cap import MarketCapFrequency, MarketCapService
from equitytrace.market.models import (
    AssetType,
    DailyPriceBar,
    MarketDataProviderName,
    MarketSymbolMapping,
    PriceAdjustmentMode,
)
from equitytrace.market.providers.errors import (
    MarketDataAuthError,
    MarketDataSymbolError,
    MarketDataValidationError,
)
from equitytrace.market.providers.twelve_data import (
    MinuteRateLimiter,
    TwelveDataProvider,
    date_windows,
    parse_retry_after,
    validate_ohlcv,
)
from equitytrace.market.service import MarketDataService
from equitytrace.market.shares import SharesOutstandingService
from equitytrace.models import FinancialFact, Issuer, Security
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository

runner = CliRunner()


def _settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = "test-key",
    enable_cache: bool = False,
    rpm: float = 1000.0,
    retries: int = 3,
) -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", key)
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "audit.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_CACHE_DIR", str(tmp_path / "mcache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true" if enable_cache else "false")
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_REQUESTS_PER_MINUTE", str(rpm))
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_MAX_RETRIES", str(retries))
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _bar(
    day: str,
    close: str = "10",
    *,
    volume: str = "100",
) -> dict[str, str]:
    return {
        "datetime": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def _payload(days: list[str], *, close: str = "10") -> dict[str, object]:
    return {
        "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
        "status": "ok",
        "values": [_bar(d, close) for d in days],
    }


def test_outputsize_omitted_when_start_and_end_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(urlparse(str(request.url)).query))
        return httpx.Response(200, json=_payload(["2023-01-03"]))

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 10), PriceAdjustmentMode.NONE
        )
    assert seen
    assert "outputsize" not in seen[0]
    assert "apikey" in seen[0]
    assert seen[0]["start_date"] == ["2023-01-01"]
    assert seen[0]["end_date"] == ["2023-01-10"]


def test_multi_window_overlap_dedup_and_empty_middle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    calls: list[tuple[str, str]] = []
    call_n = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(request.url)).query)
        start = q["start_date"][0]
        end = q["end_date"][0]
        calls.append((start, end))
        call_n["n"] += 1
        # Empty the second window so pagination continues without aborting.
        if call_n["n"] == 2:
            return httpx.Response(
                200,
                json={
                    "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
                    "status": "ok",
                    "values": [],
                },
            )
        # Return endpoints of each window so overlaps collide on boundary dates.
        values = [_bar(start), _bar(end)]
        return httpx.Response(
            200,
            json={
                "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
                "status": "ok",
                "values": values,
            },
        )

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars(
            "AAPL", date(2020, 1, 1), date(2023, 6, 1), PriceAdjustmentMode.NONE
        )
    assert len(calls) >= 3
    dates = [b.trading_date for b in result.bars]
    assert dates == sorted(set(dates))
    assert date(2020, 1, 1) in dates
    assert date(2023, 6, 1) in dates


def test_long_range_windows_terminate() -> None:
    windows = date_windows(date(2000, 1, 1), date(2026, 1, 1), 400)
    assert windows[0][0] == date(2000, 1, 1)
    assert windows[-1][1] == date(2026, 1, 1)
    # Strict forward progress: each window start advances.
    starts = [w[0] for w in windows]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))


def test_retry_after_http_date_and_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("9999") == 60.0  # capped
    assert parse_retry_after("not-a-date") is None
    assert parse_retry_after(None) is None
    future = datetime.now(UTC) + timedelta(seconds=120)
    assert parse_retry_after(format_datetime(future)) == 60.0

    settings = _settings(tmp_path, monkeypatch, retries=3)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=1))},
            )
        return httpx.Response(200, json=_payload(["2023-01-03"]))

    sleeps: list[float] = []
    limiter = MinuteRateLimiter(10_000, sleep_fn=sleeps.append)
    with TwelveDataProvider(
        settings,
        transport=httpx.MockTransport(handler),
        rate_limiter=limiter,
        sleep_fn=sleeps.append,
    ) as provider:
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
    assert attempts["n"] == 2


def test_no_retry_on_invalid_symbol_or_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, retries=5)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(
            200, json={"status": "error", "code": 400, "message": "Invalid symbol XYZ"}
        )

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        pytest.raises(MarketDataSymbolError),
    ):
        provider.fetch_daily_bars(
            "XYZ", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    assert attempts["n"] == 1

    attempts["n"] = 0

    def auth_handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, text="denied")

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(auth_handler)) as provider,
        pytest.raises(MarketDataAuthError),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    assert attempts["n"] == 1


def test_body_500_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch, retries=3)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(200, json={"status": "error", "code": 500, "message": "upstream"})
        return httpx.Response(200, json=_payload(["2023-01-03"]))

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        bars = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
    assert attempts["n"] == 3
    assert bars.bars


def test_rate_limiter_counts_live_not_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, enable_cache=True, rpm=30.0)
    calls = {"n": 0}
    sleeps: list[float] = []
    clock = {"t": 1000.0}

    def mono() -> float:
        return clock["t"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_payload(["2023-01-03"]))

    limiter = MinuteRateLimiter(30.0, sleep_fn=sleep, monotonic_fn=mono)
    with TwelveDataProvider(
        settings,
        transport=httpx.MockTransport(handler),
        rate_limiter=limiter,
    ) as provider:
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
        # Second identical request should hit cache and not consume rate slots.
        before = limiter.wait_count
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
        after = limiter.wait_count
    assert calls["n"] == 1
    assert after == before


def test_cache_excludes_api_key_and_rejects_error_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, enable_cache=True, key="super-secret-key")
    cache_dir = settings.market_data_cache_dir

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "code": 400, "message": "Invalid symbol"},
        )

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(error_handler)) as provider,
        pytest.raises(MarketDataSymbolError),
    ):
        provider.fetch_daily_bars(
            "ZZZ", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    # Error payloads must not be cached as success.
    files = list(cache_dir.glob("td_*.json"))
    assert files == []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload(["2023-01-03"]))

    with TwelveDataProvider(settings, transport=httpx.MockTransport(ok_handler)) as provider:
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
    files = list(cache_dir.glob("td_*.json"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "super-secret-key" not in text
    assert "apikey" not in text

    # Corrupt cache is ignored and replaced.
    files[0].write_text("{not-json", encoding="utf-8")
    with TwelveDataProvider(settings, transport=httpx.MockTransport(ok_handler)) as provider:
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
    assert json_loads_ok(files[0].read_text(encoding="utf-8"))


def json_loads_ok(text: str) -> bool:
    import json

    json.loads(text)
    return True


def test_nan_inf_boolean_blank_rejected() -> None:
    assert "non_positive_price" in validate_ohlcv(
        open_=Decimal("0"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=0,
    )
    from equitytrace.market.providers.twelve_data import parse_bar

    with pytest.raises(MarketDataValidationError):
        parse_bar(
            {
                "datetime": "2023-01-03",
                "open": "NaN",
                "high": "1",
                "low": "1",
                "close": "1",
                "volume": "1",
            },
            currency="USD",
            exchange_timezone="America/New_York",
        )
    with pytest.raises(MarketDataValidationError):
        parse_bar(
            {
                "datetime": "2023-01-03",
                "open": "1",
                "high": "1",
                "low": "1",
                "close": "1",
                "volume": "1.5",
            },
            currency="USD",
            exchange_timezone="America/New_York",
        )
    with pytest.raises(MarketDataValidationError):
        parse_bar(
            {
                "datetime": "2023-01-03",
                "open": True,
                "high": "1",
                "low": "1",
                "close": "1",
                "volume": "1",
            },
            currency="USD",
            exchange_timezone="America/New_York",
        )


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(MarketDataValidationError):
        bar_available_at(date(2023, 1, 3), "Not/A_Zone")
    with pytest.raises(MarketDataValidationError):
        bar_available_at(date(2023, 1, 3), "")


def test_dst_transition_and_utc_zone() -> None:
    # US spring forward 2023-03-12; 16:15 still valid local civil time.
    spring = bar_available_at(date(2023, 3, 13), "America/New_York")
    assert spring.astimezone(ZoneInfo("America/New_York")).hour == 16
    utc_bar = bar_available_at(date(2023, 1, 3), "UTC")
    assert utc_bar == datetime(2023, 1, 3, 16, 15, tzinfo=UTC)
    tokyo = bar_available_at(date(2023, 1, 3), "Asia/Tokyo")
    assert tokyo == datetime(2023, 1, 3, 7, 15, tzinfo=UTC)


def test_decimal_round_trip_precision(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "dec.duckdb")
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("DEC")
        # Exactly 6 fractional digits stored via DECIMAL(18, 6) / string path.
        weird = Decimal("99.000001")
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 1, 3),
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=weird,
                    high=weird,
                    low=weird,
                    close=weird,
                    volume=42,
                    available_at=bar_available_at(date(2023, 1, 3), "America/New_York"),
                    fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
                )
            ]
        )
        bar = repo.get_latest_price_on_or_before(instrument.instrument_id, date(2023, 1, 3))
        assert bar is not None
        assert bar.close == weird
        assert format(bar.close, "f") == "99.000001"
        col_type = conn.execute(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'daily_price_bars' AND column_name = 'close'
            """
        ).fetchone()
        assert col_type is not None
        assert "DECIMAL" in str(col_type[0]).upper()


def test_historical_provider_symbol_reuse(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "map.duckdb")
    with db.session() as conn:
        repo = MarketRepository(conn)
        old = repo.get_or_create_instrument("OLDCO", asset_type=AssetType.EQUITY)
        new = repo.get_or_create_instrument("NEWCO", asset_type=AssetType.EQUITY)
        repo.upsert_symbol_mapping(
            MarketSymbolMapping(
                instrument_id=old.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                provider_symbol="TICK",
                valid_from=date(2010, 1, 1),
                valid_to=date(2020, 12, 31),
                is_primary=True,
            )
        )
        repo.upsert_symbol_mapping(
            MarketSymbolMapping(
                instrument_id=new.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                provider_symbol="TICK",
                valid_from=date(2021, 1, 1),
                valid_to=None,
                is_primary=True,
            )
        )
        hist = repo.get_active_symbol_mapping(
            old.instrument_id,
            MarketDataProviderName.TWELVE_DATA,
            as_of=date(2015, 6, 1),
        )
        current = repo.get_active_symbol_mapping(
            new.instrument_id,
            MarketDataProviderName.TWELVE_DATA,
            as_of=date(2022, 1, 1),
        )
        assert hist is not None and hist.provider_symbol == "TICK"
        assert current is not None and current.instrument_id == new.instrument_id
        with pytest.raises(ValueError, match="Overlapping"):
            repo.upsert_symbol_mapping(
                MarketSymbolMapping(
                    instrument_id=old.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    provider_symbol="TICK",
                    valid_from=date(2022, 1, 1),
                    valid_to=None,
                    is_primary=True,
                )
            )


def test_migration_preserves_v02_rows_and_mapping_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE issuers (
            cik VARCHAR PRIMARY KEY,
            legal_name VARCHAR NOT NULL,
            entity_type VARCHAR,
            sic VARCHAR,
            sic_description VARCHAR,
            fiscal_year_end VARCHAR,
            state_of_incorporation VARCHAR,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO issuers VALUES
        ('0000320193', 'Apple', NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version VARCHAR PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            notes VARCHAR
        )
        """
    )
    conn.execute("INSERT INTO schema_migrations VALUES ('0.2.0', CURRENT_TIMESTAMP, 'v02')")
    # Old mapping PK shape without mapping_id.
    conn.execute(
        """
        CREATE TABLE market_instruments (
            instrument_id VARCHAR PRIMARY KEY,
            canonical_symbol VARCHAR NOT NULL UNIQUE,
            asset_type VARCHAR NOT NULL,
            security_ticker VARCHAR,
            issuer_cik VARCHAR,
            exchange VARCHAR,
            mic_code VARCHAR,
            currency VARCHAR NOT NULL,
            exchange_timezone VARCHAR NOT NULL,
            active BOOLEAN NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_instruments VALUES
        ('mkt_x', 'AAPL', 'equity', 'AAPL', '0000320193', NULL, NULL, 'USD',
         'America/New_York', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        CREATE TABLE market_symbol_mappings (
            instrument_id VARCHAR NOT NULL,
            provider VARCHAR NOT NULL,
            provider_symbol VARCHAR NOT NULL,
            valid_from DATE,
            valid_to DATE,
            is_primary BOOLEAN NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (provider, provider_symbol)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_symbol_mappings VALUES
        ('mkt_x', 'twelve_data', 'AAPL', NULL, NULL, TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.close()

    db = initialize_database(path)
    with db.session() as conn:
        issuer = conn.execute("SELECT legal_name FROM issuers WHERE cik = '0000320193'").fetchone()
        assert issuer is not None and issuer[0] == "Apple"
        cols = {
            str(r[0]).lower()
            for r in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'market_symbol_mappings'
                """
            ).fetchall()
        }
        assert "mapping_id" in cols
        row = conn.execute(
            "SELECT mapping_id, provider_symbol FROM market_symbol_mappings"
        ).fetchone()
        assert row is not None and row[1] == "AAPL"
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        assert "0.2.0" in versions
        assert "0.3.0-a" in versions


def test_price_as_of_boundary_microsecond(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "pit.duckdb")
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("PIT")
        available = bar_available_at(date(2023, 1, 3), "America/New_York")
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 1, 3),
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=Decimal("10"),
                    high=Decimal("10"),
                    low=Decimal("10"),
                    close=Decimal("10"),
                    volume=1,
                    available_at=available,
                    fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
                )
            ]
        )
        assert (
            repo.get_price_as_of(instrument.instrument_id, available - timedelta(microseconds=1))
            is None
        )
        assert repo.get_price_as_of(instrument.instrument_id, available) is not None


def test_split_transition_without_future_shares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point-in-time correctness wins over economic continuity."""
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0001000001", legal_name="AAA Corp"))
        SecuritiesRepository(conn).upsert_many(
            [Security(ticker="AAA", cik="0001000001", is_primary=True)]
        )
        # Pre-split shares only; post-split SEC fact not yet public on split day.
        FactsRepository(conn).upsert_many(
            [
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="CommonStockSharesOutstanding",
                    unit="shares",
                    value=1_000_000,
                    end_date=date(2023, 1, 1),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="0001000001-23-000001",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="CommonStockSharesOutstanding",
                    unit="shares",
                    value=2_000_000,
                    end_date=date(2023, 2, 10),
                    available_at=datetime(2023, 2, 20, tzinfo=UTC),  # after split price day
                    accession_number="0001000001-23-000002",
                    form="10-Q",
                    filing_date=date(2023, 2, 20),
                ),
            ]
        )
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "AAA", issuer_cik="0001000001", security_ticker="AAA"
        )
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 2, 10),
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=Decimal("50"),
                    high=Decimal("51"),
                    low=Decimal("49"),
                    close=Decimal("50"),
                    volume=10,
                    available_at=bar_available_at(date(2023, 2, 10), "America/New_York"),
                    fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
                )
            ]
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert result.is_available
    assert result.raw_close == Decimal("50")
    assert result.shares_outstanding == Decimal("1000000")
    assert result.market_cap == Decimal("50000000")
    assert result.shares_fact_date == date(2023, 1, 1)


def test_run_count_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    payload = {
        "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
        "status": "ok",
        "values": [
            _bar("2023-01-03", "10"),
            {
                "datetime": "2023-01-04",
                "open": "bad",
                "high": "2",
                "low": "1",
                "close": "1",
                "volume": "1",
            },
            _bar("2023-01-05", "11"),
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=transport)
        result = MarketDataService(conn, settings, provider=provider).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 10),
            modes=[PriceAdjustmentMode.NONE],
        )
    assert result.raw_row_count == result.inserted_row_count + result.rejected_row_count
    assert result.rejected_row_count >= 1
    assert result.inserted_row_count == 2


def test_series_multi_year_modest_perf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0001000001", legal_name="AAA"))
        SecuritiesRepository(conn).upsert_many(
            [Security(ticker="AAA", cik="0001000001", is_primary=True)]
        )
        FactsRepository(conn).upsert_many(
            [
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="CommonStockSharesOutstanding",
                    unit="shares",
                    value=1_000_000,
                    end_date=date(2020, 1, 1),
                    available_at=datetime(2020, 1, 1, tzinfo=UTC),
                    accession_number="0001000001-20-000001",
                    form="10-K",
                    filing_date=date(2020, 1, 1),
                )
            ]
        )
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "AAA", issuer_cik="0001000001", security_ticker="AAA"
        )
        bars = []
        day = date(2020, 1, 1)
        end = date(2022, 12, 31)
        fetched = datetime(2024, 1, 1, tzinfo=UTC)
        while day <= end:
            if day.weekday() < 5:
                bars.append(
                    DailyPriceBar(
                        instrument_id=instrument.instrument_id,
                        provider=MarketDataProviderName.TWELVE_DATA,
                        trading_date=day,
                        adjustment_mode=PriceAdjustmentMode.NONE,
                        open=Decimal("10"),
                        high=Decimal("10"),
                        low=Decimal("10"),
                        close=Decimal("10"),
                        volume=1,
                        available_at=bar_available_at(day, "America/New_York"),
                        fetched_at=fetched,
                    )
                )
            day += timedelta(days=1)
        repo.upsert_price_bars(bars)
        points = MarketCapService(conn).get_market_cap_series(
            "AAA",
            date(2020, 1, 1),
            date(2022, 12, 31),
            frequency=MarketCapFrequency.MONTH_END,
        )
    assert len(points) == 36
    assert all(p.result.is_available for p in points)


def test_cli_invalid_option_combinations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cli.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    clear_settings_cache()
    initialize_database(path)

    both = runner.invoke(
        app,
        [
            "market",
            "ingest",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-01-05",
            "--raw-only",
            "--adjusted-only",
        ],
    )
    assert both.exit_code != 0
    assert "Traceback" not in both.output

    order = runner.invoke(
        app,
        ["market", "ingest", "AAPL", "--start", "2023-02-01", "--end", "2023-01-01"],
    )
    assert order.exit_code != 0

    overlap = runner.invoke(
        app,
        [
            "market",
            "ingest",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-01-05",
            "--overlap-days",
            "-1",
        ],
    )
    assert overlap.exit_code != 0

    limit = runner.invoke(
        app,
        [
            "market",
            "prices",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-01-05",
            "--limit",
            "0",
        ],
    )
    assert limit.exit_code != 0

    stale = runner.invoke(
        app,
        ["market", "cap", "AAPL", "--date", "2023-01-01", "--max-price-staleness-days", "-3"],
    )
    assert stale.exit_code != 0

    freq = runner.invoke(
        app,
        [
            "market",
            "cap-series",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-02-01",
            "--frequency",
            "weekly",
        ],
    )
    assert freq.exit_code != 0


def test_prices_inspection_without_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "nokey.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    db = initialize_database(path)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_payload(["2023-01-03"]))
    )
    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=transport)
        MarketDataService(conn, settings, provider=provider).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "")
    clear_settings_cache()
    result = runner.invoke(
        app,
        [
            "market",
            "prices",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-01-05",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    assert "available_at" in result.output


def test_dei_concept_priority_and_weighted_average_excluded(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "shares.duckdb")
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0001000001", legal_name="AAA"))
        SecuritiesRepository(conn).upsert_many(
            [Security(ticker="AAA", cik="0001000001", is_primary=True)]
        )
        FactsRepository(conn).upsert_many(
            [
                FinancialFact(
                    cik="0001000001",
                    taxonomy="dei",
                    concept="EntityCommonStockSharesOutstanding",
                    unit="shares",
                    value=111,
                    end_date=date(2023, 1, 1),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="a1",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="CommonStockSharesOutstanding",
                    unit="shares",
                    value=222,
                    end_date=date(2023, 1, 1),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="a2",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="WeightedAverageNumberOfSharesOutstandingBasic",
                    unit="shares",
                    value=999,
                    end_date=date(2023, 6, 1),
                    available_at=datetime(2023, 6, 1, tzinfo=UTC),
                    accession_number="a3",
                    form="10-Q",
                    filing_date=date(2023, 6, 1),
                ),
            ]
        )
        result = SharesOutstandingService(conn).get_shares_outstanding(
            "AAA",
            market_date=date(2023, 6, 15),
            knowledge_time=datetime(2023, 6, 15, tzinfo=UTC),
        )
    assert result.shares == Decimal("111")
    assert result.concept == "EntityCommonStockSharesOutstanding"


def test_http_400_no_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch, retries=4)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, text="bad request")

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        pytest.raises(MarketDataValidationError),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 2), PriceAdjustmentMode.NONE
        )
    assert attempts["n"] == 1


def test_raw_adjusted_cache_keys_differ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch, enable_cache=True)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        q = parse_qs(urlparse(str(request.url)).query)
        close = "10" if q["adjust"][0] == "none" else "20"
        return httpx.Response(200, json=_payload(["2023-01-03"], close=close))

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        raw = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )
        adj = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.ALL
        )
    assert calls["n"] == 2
    assert raw.bars[0].close != adj.bars[0].close
    assert len(list(settings.market_data_cache_dir.glob("td_*.json"))) == 2
