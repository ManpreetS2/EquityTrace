"""Market persistence, availability, shares, and market-cap offline tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest

from equitytrace.config import Settings, clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.market.availability import bar_available_at
from equitytrace.market.market_cap import MarketCapFrequency, MarketCapService
from equitytrace.market.models import (
    AssetType,
    DailyPriceBar,
    MarketDataProviderName,
    PriceAdjustmentMode,
    make_instrument_id,
)
from equitytrace.market.service import MarketDataService
from equitytrace.market.shares import SharesOutstandingService
from equitytrace.models import FinancialFact, Issuer, Security
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository


def _settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "m.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_CACHE_DIR", str(tmp_path / "mcache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _payload(rows: list[tuple[str, str, str, str, str, str]]) -> dict[str, object]:
    return {
        "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
        "status": "ok",
        "values": [
            {
                "datetime": d,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
            }
            for d, o, h, low, c, v in rows
        ],
    }


def test_migration_from_v02_database(tmp_path) -> None:
    path = tmp_path / "legacy.duckdb"
    db = initialize_database(path)
    with db.session() as conn:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    assert "daily_price_bars" in tables
    assert "market_instruments" in tables
    assert "0.2.0" in versions
    assert "0.3.0-a" in versions


def test_instrument_mapping_and_idempotent_bars(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    rows = [
        ("2023-01-03", "100", "110", "95", "105", "1000"),
        ("2023-01-04", "105", "112", "104", "111", "1200"),
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_payload(rows)))
    with db.session() as conn:
        provider = __import__(
            "equitytrace.market.providers.twelve_data", fromlist=["TwelveDataProvider"]
        ).TwelveDataProvider(settings, transport=transport)
        service = MarketDataService(conn, settings, provider=provider)
        first = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 10),
        )
        second = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 10),
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAPL")
        assert instrument is not None
        raw = repo.get_price_bars(
            instrument.instrument_id, adjustment_mode=PriceAdjustmentMode.NONE
        )
        adj = repo.get_price_bars(instrument.instrument_id, adjustment_mode=PriceAdjustmentMode.ALL)
    assert first.inserted_row_count > 0
    assert second.unchanged_row_count > 0
    assert second.inserted_row_count == 0
    assert len(raw) == 2
    assert len(adj) == 2
    assert raw[0].adjustment_mode is PriceAdjustmentMode.NONE
    assert adj[0].adjustment_mode is PriceAdjustmentMode.ALL


def test_provider_correction_updates_row(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    first_rows = [("2023-01-03", "100", "110", "95", "105", "1000")]
    second_rows = [("2023-01-03", "100", "110", "95", "106", "1000")]
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        payload = _payload(first_rows if state["n"] <= 2 else second_rows)
        return httpx.Response(200, json=payload)

    with db.session() as conn:
        from equitytrace.market.providers.twelve_data import TwelveDataProvider

        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        service = MarketDataService(conn, settings, provider=provider)
        service.ingest("AAPL", start_date=date(2023, 1, 1), end_date=date(2023, 1, 3))
        result = service.ingest("AAPL", start_date=date(2023, 1, 1), end_date=date(2023, 1, 3))
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAPL")
        assert instrument is not None
        bars = repo.get_price_bars(
            instrument.instrument_id, adjustment_mode=PriceAdjustmentMode.NONE
        )
    assert result.updated_row_count >= 1
    assert bars[0].close == Decimal("106")


def test_spy_benchmark_without_sec_link(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json=_payload([("2023-01-03", "400", "410", "395", "405", "100")])
        )
    )
    with db.session() as conn:
        from equitytrace.market.providers.twelve_data import TwelveDataProvider

        provider = TwelveDataProvider(settings, transport=transport)
        service = MarketDataService(conn, settings, provider=provider)
        result = service.ingest(
            "SPY",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 10),
            asset_type=AssetType.ETF,
            modes=[PriceAdjustmentMode.NONE],
        )
        instrument = MarketRepository(conn).get_instrument_by_symbol("SPY")
    assert result.status.value == "success"
    assert instrument is not None
    assert instrument.asset_type is AssetType.ETF
    assert instrument.issuer_cik is None


def test_availability_1615_and_dst() -> None:
    winter = bar_available_at(date(2023, 1, 3), "America/New_York")
    summer = bar_available_at(date(2023, 7, 3), "America/New_York")
    assert winter == datetime(2023, 1, 3, 21, 15, tzinfo=UTC)
    assert summer == datetime(2023, 7, 3, 20, 15, tzinfo=UTC)
    local = winter.astimezone(ZoneInfo("America/New_York"))
    assert local.hour == 16 and local.minute == 15


def test_available_at_differs_from_fetched_at(tmp_path) -> None:
    db = initialize_database(tmp_path / "a.duckdb")
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAA")
        available = bar_available_at(date(2023, 1, 3), "America/New_York")
        fetched = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 1, 3),
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=1,
                    available_at=available,
                    fetched_at=fetched,
                )
            ]
        )
        bar = repo.get_latest_price_on_or_before(instrument.instrument_id, date(2023, 1, 3))
        assert bar is not None
        assert bar.available_at != bar.fetched_at
        before = available - timedelta(seconds=1)
        assert repo.get_price_as_of(instrument.instrument_id, before) is None
        assert repo.get_price_as_of(instrument.instrument_id, available) is not None


def _seed_shares(
    conn,
    *,
    ticker: str = "AAA",
    cik: str = "0001000001",
    value: float = 1_000_000,
    end: date = date(2023, 1, 1),
    available: datetime = datetime(2023, 1, 2, 0, 0, tzinfo=UTC),
    concept: str = "CommonStockSharesOutstanding",
    taxonomy: str = "us-gaap",
    unit: str = "shares",
    accession: str = "0001000001-23-000001",
) -> None:
    IssuersRepository(conn).upsert(Issuer(cik=cik, legal_name="AAA Corp"))
    SecuritiesRepository(conn).upsert_many([Security(ticker=ticker, cik=cik, is_primary=True)])
    FactsRepository(conn).upsert_many(
        [
            FinancialFact(
                cik=cik,
                taxonomy=taxonomy,
                concept=concept,
                unit=unit,
                value=value,
                end_date=end,
                available_at=available,
                accession_number=accession,
                form="10-K",
                filing_date=end,
            )
        ]
    )


def test_shares_excludes_future_filed_and_includes_boundary(tmp_path) -> None:
    db = initialize_database(tmp_path / "s.duckdb")
    with db.session() as conn:
        _seed_shares(
            conn,
            available=datetime(2023, 2, 1, 12, 0, tzinfo=UTC),
            end=date(2022, 12, 31),
        )
        svc = SharesOutstandingService(conn)
        missing = svc.get_shares_outstanding(
            "AAA",
            market_date=date(2023, 1, 15),
            knowledge_time=datetime(2023, 1, 15, 0, 0, tzinfo=UTC),
        )
        present = svc.get_shares_outstanding(
            "AAA",
            market_date=date(2023, 2, 2),
            knowledge_time=datetime(2023, 2, 1, 12, 0, tzinfo=UTC),
        )
    assert missing.shares is None
    assert present.shares == Decimal("1000000")


def test_weighted_average_and_non_shares_rejected(tmp_path) -> None:
    db = initialize_database(tmp_path / "w.duckdb")
    with db.session() as conn:
        _seed_shares(
            conn,
            concept="WeightedAverageNumberOfDilutedSharesOutstanding",
            taxonomy="us-gaap",
        )
        result = SharesOutstandingService(conn).get_shares_outstanding(
            "AAA",
            market_date=date(2023, 6, 1),
            knowledge_time=datetime(2023, 6, 1, tzinfo=UTC),
        )
    assert result.shares is None


def test_market_cap_split_continuity_and_raw_only(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0001000001", legal_name="AAA Corp"))
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
                    end_date=date(2023, 2, 1),
                    available_at=datetime(2023, 2, 1, tzinfo=UTC),
                    accession_number="0001000001-23-000002",
                    form="10-Q",
                    filing_date=date(2023, 2, 1),
                ),
            ]
        )
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "AAA", issuer_cik="0001000001", security_ticker="AAA"
        )
        fetched = datetime(2024, 1, 1, tzinfo=UTC)
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 1, 10),
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=10,
                    available_at=bar_available_at(date(2023, 1, 10), "America/New_York"),
                    fetched_at=fetched,
                ),
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
                    fetched_at=fetched,
                ),
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 2, 10),
                    adjustment_mode=PriceAdjustmentMode.ALL,
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=10,
                    available_at=bar_available_at(date(2023, 2, 10), "America/New_York"),
                    fetched_at=fetched,
                ),
            ]
        )
        svc = MarketCapService(conn)
        before = svc.get_market_cap("AAA", date(2023, 1, 10))
        after = svc.get_market_cap("AAA", date(2023, 2, 10))
    assert before.market_cap == Decimal("100000000")
    assert after.market_cap == Decimal("100000000")
    assert after.price_adjustment_mode is PriceAdjustmentMode.NONE
    assert after.raw_close == Decimal("50")


def test_weekend_prior_day_and_staleness(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_shares(conn, available=datetime(2023, 1, 1, tzinfo=UTC))
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "AAA", issuer_cik="0001000001", security_ticker="AAA"
        )
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 1, 6),  # Friday
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10"),
                    volume=1,
                    available_at=bar_available_at(date(2023, 1, 6), "America/New_York"),
                    fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
                )
            ]
        )
        svc = MarketCapService(conn)
        weekend = svc.get_market_cap("AAA", date(2023, 1, 8))  # Sunday
        stale = svc.get_market_cap("AAA", date(2023, 1, 20), max_price_staleness_days=7)
    assert weekend.price_date_used == date(2023, 1, 6)
    assert weekend.is_available
    assert stale.market_cap is None
    assert stale.unavailable_reason and "price_stale" in stale.unavailable_reason


def test_month_and_quarter_end_selection(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_shares(conn, available=datetime(2023, 1, 1, tzinfo=UTC))
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "AAA", issuer_cik="0001000001", security_ticker="AAA"
        )
        fetched = datetime(2024, 1, 1, tzinfo=UTC)
        for d, close in [
            (date(2023, 1, 30), "10"),
            (date(2023, 1, 31), "11"),
            (date(2023, 3, 30), "12"),
            (date(2023, 3, 31), "13"),
        ]:
            repo.upsert_price_bars(
                [
                    DailyPriceBar(
                        instrument_id=instrument.instrument_id,
                        provider=MarketDataProviderName.TWELVE_DATA,
                        trading_date=d,
                        adjustment_mode=PriceAdjustmentMode.NONE,
                        open=Decimal(close),
                        high=Decimal(close),
                        low=Decimal(close),
                        close=Decimal(close),
                        volume=1,
                        available_at=bar_available_at(d, "America/New_York"),
                        fetched_at=fetched,
                    )
                ]
            )
        svc = MarketCapService(conn)
        month = svc.get_market_cap_series(
            "AAA",
            date(2023, 1, 1),
            date(2023, 3, 31),
            frequency=MarketCapFrequency.MONTH_END,
        )
        quarter = svc.get_market_cap_series(
            "AAA",
            date(2023, 1, 1),
            date(2023, 3, 31),
            frequency=MarketCapFrequency.QUARTER_END,
        )
    assert [p.as_of_date for p in month] == [date(2023, 1, 31), date(2023, 3, 31)]
    assert [p.as_of_date for p in quarter] == [date(2023, 3, 31)]


def test_multi_class_unavailable(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0001652044", legal_name="Alphabet"))
        SecuritiesRepository(conn).upsert_many(
            [
                Security(ticker="GOOGL", cik="0001652044", is_primary=True),
                Security(ticker="GOOG", cik="0001652044", is_primary=False),
            ]
        )
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "GOOGL", issuer_cik="0001652044", security_ticker="GOOGL"
        )
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 1, 3),
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=1,
                    available_at=bar_available_at(date(2023, 1, 3), "America/New_York"),
                    fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
                )
            ]
        )
        result = MarketCapService(conn).get_market_cap("GOOGL", date(2023, 1, 3))
    assert result.market_cap is None
    assert result.unavailable_reason == "multi_class_issuer_ambiguous"


def test_later_knowledge_time_warning(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_shares(conn, available=datetime(2023, 1, 1, tzinfo=UTC))
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "AAA", issuer_cik="0001000001", security_ticker="AAA"
        )
        available = bar_available_at(date(2023, 1, 3), "America/New_York")
        repo.upsert_price_bars(
            [
                DailyPriceBar(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    trading_date=date(2023, 1, 3),
                    adjustment_mode=PriceAdjustmentMode.NONE,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10"),
                    volume=1,
                    available_at=available,
                    fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
                )
            ]
        )
        result = MarketCapService(conn).get_market_cap(
            "AAA",
            date(2023, 1, 3),
            as_of=available + timedelta(days=30),
        )
    assert result.is_available
    assert "later_knowledge_time" in result.warnings


def test_make_instrument_id_stable() -> None:
    assert make_instrument_id("aapl") == make_instrument_id("AAPL")
