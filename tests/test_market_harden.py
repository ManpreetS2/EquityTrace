"""Hardening tests for EquityTrace v0.3a market-data review fixes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from equitytrace.config import Settings, clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.market.models import (
    DailyPriceBar,
    MarketDataProviderName,
    MarketDataRunStatus,
    MarketSymbolMapping,
    PriceAdjustmentMode,
)
from equitytrace.market.providers.errors import MarketDataError, MarketDataValidationError
from equitytrace.market.providers.twelve_data import TwelveDataProvider
from equitytrace.market.service import MarketDataService
from equitytrace.models import Issuer, Security
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "h.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_CACHE_DIR", str(tmp_path / "mcache"))
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _meta(**overrides: object) -> dict[str, object]:
    meta: dict[str, object] = {
        "currency": "USD",
        "exchange_timezone": "America/New_York",
    }
    meta.update(overrides)
    return meta


def _values(*days: str, close: str = "10") -> list[dict[str, str]]:
    return [
        {
            "datetime": day,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "100",
        }
        for day in days
    ]


def _ok(days: list[str], **meta_overrides: object) -> dict[str, object]:
    return {
        "meta": _meta(**meta_overrides),
        "status": "ok",
        "values": _values(*days),
    }


class _FakeProvider:
    """Injectable provider with controllable per-mode responses and close spy."""

    name = MarketDataProviderName.TWELVE_DATA.value

    def __init__(self) -> None:
        self.closed = 0
        self.calls: list[tuple[str, PriceAdjustmentMode]] = []
        self.responses: dict[PriceAdjustmentMode, object] = {}
        self.errors: dict[PriceAdjustmentMode, Exception] = {}

    def fetch_daily_bars(
        self,
        provider_symbol: str,
        start_date: date,
        end_date: date,
        adjustment_mode: PriceAdjustmentMode,
    ):
        self.calls.append((provider_symbol, adjustment_mode))
        if adjustment_mode in self.errors:
            raise self.errors[adjustment_mode]
        response = self.responses[adjustment_mode]
        assert not isinstance(response, Exception)
        return response

    def close(self) -> None:
        self.closed += 1


def _bars_response(
    days: list[str],
    *,
    mode: PriceAdjustmentMode,
    incomplete: bool = False,
    malformed: int = 0,
    duplicate_rows: int = 0,
    currency: str = "USD",
    tz: str = "America/New_York",
    meta: dict[str, object] | None = None,
):
    from equitytrace.market.models import DailyBarsResponse, ProviderBar

    bars = tuple(
        ProviderBar(
            trading_date=date.fromisoformat(d),
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=100,
            currency=currency,
            exchange_timezone=tz,
        )
        for d in days
    )
    payload_meta = meta or {}
    if incomplete:
        payload_meta = {
            **payload_meta,
            "incomplete_interior_windows": [
                {"window_start": "2021-01-01", "window_end": "2021-06-01"}
            ],
        }
    return DailyBarsResponse(
        provider=MarketDataProviderName.TWELVE_DATA,
        provider_symbol="AAPL",
        adjustment_mode=mode,
        currency=currency,
        exchange_timezone=tz,
        bars=bars,
        meta=payload_meta,
        malformed_row_count=malformed,
        duplicate_row_count=duplicate_rows,
        incomplete=incomplete,
    )


def test_missing_and_blank_metadata_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)

    def missing_tz(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "meta": {"currency": "USD"},
                "status": "ok",
                "values": _values("2023-01-03"),
            },
        )

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(missing_tz)) as provider,
        pytest.raises(MarketDataValidationError, match="timezone"),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )

    def blank_tz(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "meta": {"currency": "USD", "exchange_timezone": "  "},
                "status": "ok",
                "values": _values("2023-01-03"),
            },
        )

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(blank_tz)) as provider,
        pytest.raises(MarketDataValidationError, match="timezone"),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )

    def invalid_tz(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(["2023-01-03"], exchange_timezone="Not/A_Zone"))

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(invalid_tz)) as provider,
        pytest.raises(MarketDataValidationError, match="unknown exchange timezone"),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )

    def missing_ccy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "meta": {"exchange_timezone": "America/New_York"},
                "status": "ok",
                "values": _values("2023-01-03"),
            },
        )

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(missing_ccy)) as provider,
        pytest.raises(MarketDataValidationError, match="currency"),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )

    def blank_ccy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(["2023-01-03"], currency=""))

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(blank_ccy)) as provider,
        pytest.raises(MarketDataValidationError, match="currency"),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
        )


def test_tokyo_and_utc_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    for tz, ccy in (("Asia/Tokyo", "JPY"), ("UTC", "USD")):
        transport = httpx.MockTransport(
            lambda request, tz=tz, ccy=ccy: httpx.Response(
                200, json=_ok(["2023-01-03"], currency=ccy, exchange_timezone=tz)
            )
        )
        with TwelveDataProvider(settings, transport=transport) as provider:
            result = provider.fetch_daily_bars(
                "X", date(2023, 1, 1), date(2023, 1, 5), PriceAdjustmentMode.NONE
            )
        assert result.currency == ccy
        assert result.exchange_timezone == tz


def test_cross_window_metadata_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    call_n = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_n["n"] += 1
        q = parse_qs(urlparse(str(request.url)).query)
        start = q["start_date"][0]
        end = q["end_date"][0]
        if call_n["n"] == 1:
            return httpx.Response(
                200, json=_ok([start, end], currency="USD", exchange_timezone="America/New_York")
            )
        return httpx.Response(
            200, json=_ok([start, end], currency="EUR", exchange_timezone="America/New_York")
        )

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider,
        pytest.raises(MarketDataValidationError, match="currency changed"),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2020, 1, 1), date(2022, 6, 1), PriceAdjustmentMode.NONE
        )

    call_n["n"] = 0

    def tz_handler(request: httpx.Request) -> httpx.Response:
        call_n["n"] += 1
        q = parse_qs(urlparse(str(request.url)).query)
        start = q["start_date"][0]
        end = q["end_date"][0]
        tz = "America/New_York" if call_n["n"] == 1 else "Asia/Tokyo"
        return httpx.Response(200, json=_ok([start, end], exchange_timezone=tz))

    with (
        TwelveDataProvider(settings, transport=httpx.MockTransport(tz_handler)) as provider,
        pytest.raises(MarketDataValidationError, match="timezone changed"),
    ):
        provider.fetch_daily_bars(
            "AAPL", date(2020, 1, 1), date(2022, 6, 1), PriceAdjustmentMode.NONE
        )


def test_empty_first_and_final_windows_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    call_n = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_n["n"] += 1
        q = parse_qs(urlparse(str(request.url)).query)
        start = q["start_date"][0]
        end = q["end_date"][0]
        # First and last empty; middle populated.
        total_guess = 3
        if call_n["n"] == 1 or call_n["n"] >= total_guess:
            return httpx.Response(
                200,
                json={"meta": _meta(), "status": "ok", "values": []},
            )
        return httpx.Response(200, json=_ok([start, end]))

    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars(
            "AAPL", date(2020, 1, 1), date(2023, 6, 1), PriceAdjustmentMode.NONE
        )
    assert result.bars
    assert result.incomplete is False


def test_mapping_resolution_and_overlap_invariants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        service = MarketDataService(conn, settings, provider=fake)
        first = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        second = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAPL")
        assert instrument is not None
        mappings = repo.list_primary_mappings(
            instrument.instrument_id, MarketDataProviderName.TWELVE_DATA
        )
        assert len(mappings) == 1
        assert first.provider_symbol == "AAPL"
        assert second.provider_symbol == "AAPL"

        # Overlapping primary mappings for same instrument/provider rejected.
        with pytest.raises(ValueError, match="Overlapping primary"):
            repo.upsert_symbol_mapping(
                MarketSymbolMapping(
                    instrument_id=instrument.instrument_id,
                    provider=MarketDataProviderName.TWELVE_DATA,
                    provider_symbol="AAPL.US",
                    is_primary=True,
                )
            )

        # Historical dated mapping + open-ended later mapping.
        repo._conn.execute("DELETE FROM market_symbol_mappings")
        repo.upsert_symbol_mapping(
            MarketSymbolMapping(
                instrument_id=instrument.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                provider_symbol="OLD",
                valid_from=date(2010, 1, 1),
                valid_to=date(2020, 12, 31),
                is_primary=True,
            )
        )
        repo.upsert_symbol_mapping(
            MarketSymbolMapping(
                instrument_id=instrument.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                provider_symbol="NEW",
                valid_from=date(2021, 1, 1),
                valid_to=None,
                is_primary=True,
            )
        )
        with pytest.raises(MarketDataError, match="spans multiple provider"):
            service.ingest(
                "AAPL",
                start_date=date(2020, 6, 1),
                end_date=date(2021, 6, 1),
                modes=[PriceAdjustmentMode.NONE],
            )
        # Date-selected mapping.
        fake.calls.clear()
        service.ingest(
            "AAPL",
            start_date=date(2019, 1, 1),
            end_date=date(2019, 6, 1),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert fake.calls[0][0] == "OLD"


def test_explicit_provider_symbol_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        service = MarketDataService(conn, settings, provider=fake)
        service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        with pytest.raises(MarketDataError, match="conflicts with active mapping"):
            service.ingest(
                "AAPL",
                start_date=date(2023, 1, 1),
                end_date=date(2023, 1, 5),
                provider_symbol="AAPL.US",
                modes=[PriceAdjustmentMode.NONE],
            )


def test_partial_when_adjusted_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    fake.errors[PriceAdjustmentMode.ALL] = MarketDataError("adjusted boom")
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAPL")
        assert instrument is not None
        raw = repo.get_price_bars(
            instrument.instrument_id, adjustment_mode=PriceAdjustmentMode.NONE
        )
        adj = repo.get_price_bars(instrument.instrument_id, adjustment_mode=PriceAdjustmentMode.ALL)
    assert result.status is MarketDataRunStatus.PARTIAL
    assert result.error_summary and "all:" in result.error_summary
    assert len(raw) == 1
    assert adj == []
    assert result.inserted_row_count == 1


def test_failed_when_no_mode_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.errors[PriceAdjustmentMode.NONE] = MarketDataError("raw boom")
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
    assert result.status is MarketDataRunStatus.FAILED
    assert result.inserted_row_count == 0


def test_success_both_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    fake.responses[PriceAdjustmentMode.ALL] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.ALL
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
    assert result.status is MarketDataRunStatus.SUCCESS
    assert (
        result.raw_row_count
        == result.inserted_row_count
        + result.updated_row_count
        + result.unchanged_row_count
        + result.rejected_row_count
    )


def test_db_error_rolls_back_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03", "2023-01-04"], mode=PriceAdjustmentMode.NONE
    )

    with db.session() as conn:
        repo = MarketRepository(conn)
        original = repo.upsert_price_bars

        def boom(bars: list[DailyPriceBar]):
            if bars:
                original(bars[:1])
                raise RuntimeError("simulated db failure")
            return original(bars)

        monkeypatch.setattr(repo, "upsert_price_bars", boom)
        # Rebind service repo after monkeypatch on the instance used by atomic helper.
        service = MarketDataService(conn, settings, provider=fake)
        service._repo = repo

        def atomic(bars: list[DailyPriceBar]):
            conn.execute("BEGIN TRANSACTION")
            try:
                counts = repo.upsert_price_bars(bars)
                conn.execute("COMMIT")
                return counts
            except Exception:
                conn.execute("ROLLBACK")
                raise

        monkeypatch.setattr(repo, "upsert_price_bars_atomic", atomic)
        result = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        instrument = repo.get_instrument_by_symbol("AAPL")
        assert instrument is not None
        bars = repo.get_price_bars(
            instrument.instrument_id, adjustment_mode=PriceAdjustmentMode.NONE
        )
    assert result.status is MarketDataRunStatus.FAILED
    assert bars == []


def test_incomplete_interior_marks_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE, incomplete=True
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
    assert result.status is MarketDataRunStatus.PARTIAL
    assert any("incomplete_interior_windows" in w for w in result.warnings)
    assert result.inserted_row_count == 1


def test_instrument_enrichment_after_sec_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        # Price-only instrument first.
        service = MarketDataService(conn, settings, provider=fake)
        service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        repo = MarketRepository(conn)
        before = repo.get_instrument_by_symbol("AAPL")
        assert before is not None
        assert before.issuer_cik is None
        price_count = len(
            repo.get_price_bars(before.instrument_id, adjustment_mode=PriceAdjustmentMode.NONE)
        )

        IssuersRepository(conn).upsert(Issuer(cik="0000320193", legal_name="Apple"))
        SecuritiesRepository(conn).upsert_many(
            [Security(ticker="AAPL", cik="0000320193", exchange="NASDAQ", is_primary=True)]
        )
        service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        after = repo.get_instrument_by_symbol("AAPL")
        assert after is not None
        assert after.instrument_id == before.instrument_id
        assert after.issuer_cik == "0000320193"
        assert after.exchange == "NASDAQ"
        assert (
            len(repo.get_price_bars(after.instrument_id, adjustment_mode=PriceAdjustmentMode.NONE))
            == price_count
        )

        with pytest.raises(ValueError, match="linked to CIK"):
            repo.enrich_instrument(after, issuer_cik="0000000999")


def test_blank_exchange_does_not_erase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = initialize_database(tmp_path / "e.duckdb")
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAA", exchange="NASDAQ")
        enriched = repo.enrich_instrument(instrument, exchange="")
        assert enriched.exchange == "NASDAQ"


def test_owned_provider_closed_on_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    closed = {"n": 0}

    class SpyProvider(TwelveDataProvider):
        def close(self) -> None:  # type: ignore[override]
            closed["n"] += 1
            super().close()

    def factory(settings_obj: Settings) -> TwelveDataProvider:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=_ok(["2023-01-03"]))
        )
        return SpyProvider(settings_obj, transport=transport)

    monkeypatch.setattr(
        "equitytrace.market.service.TwelveDataProvider",
        factory,
    )
    with db.session() as conn:
        # Internally created provider should be closed.
        MarketDataService(conn, settings).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
    assert closed["n"] == 1

    # Injected provider remains caller-owned.
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        MarketDataService(conn, settings, provider=fake).ingest(
            "MSFT",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
    assert fake.closed == 0


def test_reingest_after_partial_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    fake.errors[PriceAdjustmentMode.ALL] = MarketDataError("adjusted boom")
    with db.session() as conn:
        service = MarketDataService(conn, settings, provider=fake)
        partial = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        assert partial.status is MarketDataRunStatus.PARTIAL
        del fake.errors[PriceAdjustmentMode.ALL]
        fake.responses[PriceAdjustmentMode.ALL] = _bars_response(
            ["2023-01-03"], mode=PriceAdjustmentMode.ALL
        )
        done = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAPL")
        assert instrument is not None
        assert (
            len(
                repo.get_price_bars(
                    instrument.instrument_id, adjustment_mode=PriceAdjustmentMode.ALL
                )
            )
            == 1
        )
    assert done.status is MarketDataRunStatus.SUCCESS


def test_duplicate_count_reconciliation_via_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"],
        mode=PriceAdjustmentMode.NONE,
        duplicate_rows=2,
        malformed=1,
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
    assert (
        result.raw_row_count
        == result.inserted_row_count
        + result.updated_row_count
        + result.unchanged_row_count
        + result.rejected_row_count
    )
    assert result.rejected_row_count == 3
    assert result.status is MarketDataRunStatus.PARTIAL
