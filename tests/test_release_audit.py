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


def test_date_only_acceptance_uses_filing_eod_fallback() -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from equitytrace.sec.normalization import (
        parse_acceptance_datetime,
        resolve_available_at,
    )

    eastern = ZoneInfo("America/New_York")
    for raw in ("2024-02-15", "20240215"):
        parsed = parse_acceptance_datetime(raw)
        assert parsed is None
        available = resolve_available_at(
            acceptance_datetime=parsed,
            filing_date=date(2024, 2, 15),
        )
        assert available == datetime(2024, 2, 15, 23, 59, 59, tzinfo=eastern).astimezone(UTC)

    # Explicit midnight with a clock token remains Eastern wall time.
    parsed = parse_acceptance_datetime("2024-02-15 00:00:00")
    assert parsed == datetime(2024, 2, 15, 5, 0, 0, tzinfo=UTC)


def test_cross_window_within_window_conflict_evicts_earlier_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from equitytrace.market.providers import twelve_data as td
    from equitytrace.market.providers.twelve_data import TwelveDataProvider

    settings = _settings(tmp_path, monkeypatch, EQUITYTRACE_ENABLE_CACHE="false")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
                    "status": "ok",
                    "values": [
                        {
                            "datetime": "2020-01-02",
                            "open": "10",
                            "high": "10",
                            "low": "10",
                            "close": "10",
                            "volume": "1",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
                "status": "ok",
                "values": [
                    {
                        "datetime": "2020-01-02",
                        "open": "10",
                        "high": "10",
                        "low": "10",
                        "close": "11",
                        "volume": "1",
                    },
                    {
                        "datetime": "2020-01-02",
                        "open": "10",
                        "high": "10",
                        "low": "10",
                        "close": "12",
                        "volume": "1",
                    },
                    {
                        "datetime": "2020-01-03",
                        "open": "10",
                        "high": "10",
                        "low": "10",
                        "close": "10",
                        "volume": "1",
                    },
                ],
            },
        )

    orig = td.date_windows
    td.date_windows = lambda start, end, window_days: [  # type: ignore[misc]
        (date(2020, 1, 1), date(2020, 1, 2)),
        (date(2020, 1, 2), date(2020, 1, 5)),
    ]
    try:
        with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
            response = provider.fetch_daily_bars(
                "X",
                date(2020, 1, 1),
                date(2020, 1, 5),
                PriceAdjustmentMode.NONE,
            )
    finally:
        td.date_windows = orig

    assert [b.trading_date for b in response.bars] == [date(2020, 1, 3)]
    assert response.conflicting_duplicate_dates == (date(2020, 1, 2),)


def test_multi_class_unavailable_without_instrument_cik_link(tmp_path: Path) -> None:
    from equitytrace.market.market_cap import MarketCapService
    from equitytrace.market.models import make_instrument_id
    from equitytrace.models import Issuer, Security
    from equitytrace.repositories.issuers import IssuersRepository
    from equitytrace.repositories.market import MarketRepository
    from equitytrace.repositories.securities import SecuritiesRepository

    db = initialize_database(tmp_path / "multi.duckdb")
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0001652044", legal_name="Alphabet"))
        SecuritiesRepository(conn).upsert_many(
            [
                Security(ticker="GOOGL", cik="0001652044", is_primary=True),
                Security(ticker="GOOG", cik="0001652044", is_primary=False),
            ]
        )
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("GOOGL", issuer_cik=None)
        assert instrument.issuer_cik is None
        assert instrument.instrument_id == make_instrument_id("GOOGL")
        result = MarketCapService(conn).get_market_cap("GOOGL", date(2023, 1, 3))
    assert result.market_cap is None
    assert result.unavailable_reason == "multi_class_issuer_ambiguous"


def test_inverted_mapping_interval_rejected(tmp_path: Path) -> None:
    from equitytrace.market.models import MarketSymbolMapping, make_instrument_id
    from equitytrace.repositories.market import MarketRepository

    db = initialize_database(tmp_path / "map.duckdb")
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAA")
        with pytest.raises(ValueError, match=r"valid_from .* is after valid_to"):
            repo.upsert_symbol_mapping(
                MarketSymbolMapping(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    provider_symbol="AAA",
                    valid_from=date(2024, 1, 10),
                    valid_to=date(2024, 1, 1),
                    is_primary=True,
                )
            )
        assert instrument.instrument_id == make_instrument_id("AAA")


def test_cli_failed_ingest_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from equitytrace.cli import app
    from equitytrace.market.models import MarketDataIngestionResult, MarketDataRunStatus
    from equitytrace.market.service import MarketDataService

    settings = _settings(tmp_path, monkeypatch)
    initialize_database(settings.database_path)

    def fake_ingest(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del self, args, kwargs
        return MarketDataIngestionResult(
            run_id="run",
            provider=MarketDataProviderName.TWELVE_DATA,
            instrument_id="i",
            canonical_symbol="ZZZ",
            provider_symbol="ZZZ",
            requested_start_date=date(2023, 1, 1),
            requested_end_date=date(2023, 1, 31),
            adjustment_modes=(PriceAdjustmentMode.NONE,),
            status=MarketDataRunStatus.FAILED,
            raw_row_count=3,
            inserted_row_count=0,
            updated_row_count=0,
            unchanged_row_count=0,
            rejected_row_count=3,
            stored_start_date=None,
            stored_end_date=None,
            error_summary=None,
            warnings=(),
        )

    monkeypatch.setattr(MarketDataService, "ingest", fake_ingest)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "market",
            "ingest",
            "ZZZ",
            "--start",
            "2023-01-01",
            "--end",
            "2023-01-31",
            "--raw-only",
            "--database",
            str(settings.database_path),
        ],
    )
    assert result.exit_code != 0
    assert "failed" in result.output.lower() or "Error:" in result.output


def test_market_run_finalize_failure_is_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider(
        DailyBarsResponse(
            provider=MarketDataProviderName.TWELVE_DATA,
            provider_symbol="FIN",
            adjustment_mode=PriceAdjustmentMode.NONE,
            currency="USD",
            exchange_timezone="America/New_York",
            bars=(_bar("2023-01-03"),),
        )
    )
    with db.session() as conn:
        service = MarketDataService(conn, settings, provider=fake)

        def boom(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("disk full")

        monkeypatch.setattr(service._repo, "complete_market_data_run", boom)
        result = service.ingest(
            "FIN",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 31),
            modes=[PriceAdjustmentMode.NONE],
        )
        stored = conn.execute("SELECT COUNT(*) FROM daily_price_bars").fetchone()

    assert result.status is not MarketDataRunStatus.SUCCESS
    assert result.error_summary is not None
    assert "run_finalize_failed" in result.error_summary
    assert "disk full" in result.error_summary
    assert stored is not None and int(stored[0]) == 1
