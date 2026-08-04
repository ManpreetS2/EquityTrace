"""Final correctness tests for EquityTrace v0.3a market-data edge cases."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from equitytrace.config import Settings, clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.market.models import (
    DailyBarsResponse,
    DailyPriceBar,
    MarketDataProviderName,
    MarketDataRunStatus,
    MarketSymbolMapping,
    PriceAdjustmentMode,
    ProviderBar,
)
from equitytrace.market.providers.errors import MarketDataError
from equitytrace.market.providers.twelve_data import TwelveDataProvider
from equitytrace.market.service import MarketDataService, _finalize_status
from equitytrace.models import Issuer, Security
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "final.duckdb"))
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
        return self.responses[adjustment_mode]

    def close(self) -> None:
        self.closed += 1


def _bars_response(
    days: list[str],
    *,
    mode: PriceAdjustmentMode,
    incomplete: bool = False,
    malformed: int = 0,
    duplicate_rows: int = 0,
    conflicting: tuple[date, ...] = (),
    currency: str = "USD",
    tz: str = "America/New_York",
    close: str = "10",
    meta: dict[str, object] | None = None,
) -> DailyBarsResponse:
    bars = tuple(
        ProviderBar(
            trading_date=date.fromisoformat(d),
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=100,
            currency=currency,
            exchange_timezone=tz,
        )
        for d in days
    )
    payload_meta = dict(meta or {})
    if incomplete:
        payload_meta["incomplete_interior_windows"] = [
            {"window_start": "2021-01-01", "window_end": "2021-06-01"}
        ]
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
        conflicting_duplicate_dates=conflicting,
        incomplete=incomplete,
    )


def _seed_mapping(
    repo: MarketRepository,
    instrument_id: str,
    symbol: str,
    *,
    valid_from: date | None,
    valid_to: date | None,
) -> None:
    repo.upsert_symbol_mapping(
        MarketSymbolMapping(
            instrument_id=instrument_id,
            provider=MarketDataProviderName.TWELVE_DATA,
            provider_symbol=symbol,
            valid_from=valid_from,
            valid_to=valid_to,
            is_primary=True,
        )
    )


def test_mapping_request_before_valid_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "AAPL",
            valid_from=date(2020, 1, 1),
            valid_to=None,
        )
        with pytest.raises(MarketDataError, match="covers requested start"):
            MarketDataService(conn, settings, provider=fake).ingest(
                "AAPL",
                start_date=date(2019, 6, 1),
                end_date=date(2020, 6, 1),
                modes=[PriceAdjustmentMode.NONE],
            )


def test_mapping_request_after_valid_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "OLD",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        with pytest.raises(MarketDataError, match="mapping gap"):
            MarketDataService(conn, settings, provider=fake).ingest(
                "AAPL",
                start_date=date(2020, 6, 1),
                end_date=date(2021, 6, 1),
                modes=[PriceAdjustmentMode.NONE],
            )


def test_mapping_gap_between_mappings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "AAPL",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "AAPL",
            valid_from=date(2021, 2, 1),
            valid_to=None,
        )
        with pytest.raises(MarketDataError, match="mapping gap"):
            MarketDataService(conn, settings, provider=fake).ingest(
                "AAPL",
                start_date=date(2020, 6, 1),
                end_date=date(2021, 6, 1),
                modes=[PriceAdjustmentMode.NONE],
            )


def test_mapping_adjacent_different_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "OLD",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "NEW",
            valid_from=date(2021, 1, 1),
            valid_to=None,
        )
        with pytest.raises(MarketDataError, match="spans multiple provider"):
            MarketDataService(conn, settings, provider=fake).ingest(
                "AAPL",
                start_date=date(2020, 6, 1),
                end_date=date(2021, 6, 1),
                modes=[PriceAdjustmentMode.NONE],
            )


def test_mapping_adjacent_same_symbol_no_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2020-12-31", "2021-01-04"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "AAPL",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "AAPL",
            valid_from=date(2021, 1, 1),
            valid_to=None,
        )
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2020, 6, 1),
            end_date=date(2021, 6, 1),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.provider_symbol == "AAPL"
        assert result.status is MarketDataRunStatus.SUCCESS


def test_mapping_exact_valid_from_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2020-01-01"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "BOUND",
            valid_from=date(2020, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.provider_symbol == "BOUND"
        assert fake.calls[0][0] == "BOUND"


def test_mapping_exact_valid_to_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2020-12-31"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "BOUND",
            valid_from=date(2020, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2020, 12, 30),
            end_date=date(2020, 12, 31),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.provider_symbol == "BOUND"


def test_mapping_fully_covered_by_historical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2019-06-03"], mode=PriceAdjustmentMode.NONE
    )
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "OLD",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "NEW",
            valid_from=date(2021, 1, 1),
            valid_to=None,
        )
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2019, 1, 1),
            end_date=date(2019, 12, 31),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.provider_symbol == "OLD"
        assert fake.calls[0][0] == "OLD"


def _spy_factory(settings_obj: Settings, *, closed: dict[str, int], boom: bool = False):
    class SpyProvider(TwelveDataProvider):
        def close(self) -> None:  # type: ignore[override]
            closed["n"] += 1
            super().close()

        def fetch_daily_bars(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if boom:
                raise MarketDataError("fetch boom")
            return super().fetch_daily_bars(*args, **kwargs)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_ok(["2023-01-03"])))
    return SpyProvider(settings_obj, transport=transport)


def test_owned_provider_closed_on_invalid_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    closed = {"n": 0}
    monkeypatch.setattr(
        "equitytrace.market.service.TwelveDataProvider",
        lambda s: _spy_factory(s, closed=closed),
    )
    with db.session() as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument("AAPL")
        _seed_mapping(
            repo,
            instrument.instrument_id,
            "OLD",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 12, 31),
        )
        with pytest.raises(MarketDataError, match="mapping gap"):
            MarketDataService(conn, settings).ingest(
                "AAPL",
                start_date=date(2020, 6, 1),
                end_date=date(2021, 6, 1),
                modes=[PriceAdjustmentMode.NONE],
            )
    assert closed["n"] == 1


def test_owned_provider_closed_on_instrument_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    closed = {"n": 0}
    monkeypatch.setattr(
        "equitytrace.market.service.TwelveDataProvider",
        lambda s: _spy_factory(s, closed=closed),
    )
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0000320193", legal_name="Apple"))
        IssuersRepository(conn).upsert(Issuer(cik="0000000999", legal_name="Other"))
        SecuritiesRepository(conn).upsert_many(
            [Security(ticker="AAPL", cik="0000000999", exchange="NYSE", is_primary=True)]
        )
        MarketRepository(conn).get_or_create_instrument(
            "AAPL", issuer_cik="0000320193", security_ticker="AAPL"
        )
        with pytest.raises(MarketDataError, match="linked to CIK"):
            MarketDataService(conn, settings).ingest(
                "AAPL",
                start_date=date(2023, 1, 1),
                end_date=date(2023, 1, 5),
                modes=[PriceAdjustmentMode.NONE],
            )
    assert closed["n"] == 1


def test_owned_provider_closed_on_run_creation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    closed = {"n": 0}
    monkeypatch.setattr(
        "equitytrace.market.service.TwelveDataProvider",
        lambda s: _spy_factory(s, closed=closed),
    )

    def boom_create(*args: object, **kwargs: object) -> str:
        raise RuntimeError("run boom")

    with db.session() as conn:
        monkeypatch.setattr(MarketRepository, "create_market_data_run", boom_create)
        with pytest.raises(RuntimeError, match="run boom"):
            MarketDataService(conn, settings).ingest(
                "AAPL",
                start_date=date(2023, 1, 1),
                end_date=date(2023, 1, 5),
                modes=[PriceAdjustmentMode.NONE],
            )
    assert closed["n"] == 1


def test_owned_provider_closed_on_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    closed = {"n": 0}
    monkeypatch.setattr(
        "equitytrace.market.service.TwelveDataProvider",
        lambda s: _spy_factory(s, closed=closed, boom=True),
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.status is MarketDataRunStatus.FAILED
    assert closed["n"] == 1


def test_owned_provider_closed_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    closed = {"n": 0}
    monkeypatch.setattr(
        "equitytrace.market.service.TwelveDataProvider",
        lambda s: _spy_factory(s, closed=closed),
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.status is MarketDataRunStatus.SUCCESS
    assert closed["n"] == 1


def test_finalize_one_mode_all_rejected_failed() -> None:
    assert (
        _finalize_status(
            modes_requested=1,
            modes_clean=0,
            modes_with_committed_rows=0,
            any_rejected=True,
            any_incomplete=False,
        )
        is MarketDataRunStatus.FAILED
    )


def test_one_mode_all_rows_rejected_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        [],
        mode=PriceAdjustmentMode.NONE,
        malformed=3,
        duplicate_rows=1,
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.status is MarketDataRunStatus.FAILED
        assert result.inserted_row_count == 0
        assert result.rejected_row_count == 4
        assert result.raw_row_count == 4


def test_two_modes_both_all_rejected_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    for mode in (PriceAdjustmentMode.NONE, PriceAdjustmentMode.ALL):
        fake.responses[mode] = _bars_response([], mode=mode, malformed=2)
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        assert result.status is MarketDataRunStatus.FAILED
        assert result.rejected_row_count == 4
        assert result.raw_row_count == 4


def test_raw_valid_adjusted_all_rejected_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE
    )
    fake.responses[PriceAdjustmentMode.ALL] = _bars_response(
        [], mode=PriceAdjustmentMode.ALL, malformed=2
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        assert result.status is MarketDataRunStatus.PARTIAL
        assert result.inserted_row_count == 1
        assert result.rejected_row_count == 2
        assert (
            result.raw_row_count
            == result.inserted_row_count
            + result.updated_row_count
            + result.unchanged_row_count
            + result.rejected_row_count
        )


def test_raw_unchanged_adjusted_fails_partial(
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
        assert first.status is MarketDataRunStatus.SUCCESS
        fake.errors[PriceAdjustmentMode.ALL] = MarketDataError("adjusted boom")
        second = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        assert second.status is MarketDataRunStatus.PARTIAL
        assert second.unchanged_row_count == 1
        assert second.inserted_row_count == 0


def test_all_modes_clean_unchanged_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    for mode in (PriceAdjustmentMode.NONE, PriceAdjustmentMode.ALL):
        fake.responses[mode] = _bars_response(["2023-01-03"], mode=mode)
    with db.session() as conn:
        service = MarketDataService(conn, settings, provider=fake)
        first = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        assert first.status is MarketDataRunStatus.SUCCESS
        second = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
        )
        assert second.status is MarketDataRunStatus.SUCCESS
        assert second.unchanged_row_count == 2
        assert second.inserted_row_count == 0
        assert second.rejected_row_count == 0


def test_empty_accepted_only_malformed_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        [], mode=PriceAdjustmentMode.NONE, malformed=5
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.status is MarketDataRunStatus.FAILED
        assert result.raw_row_count == result.rejected_row_count == 5


def _patch_small_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("equitytrace.market.providers.twelve_data._WINDOW_DAYS", 5)
    monkeypatch.setattr("equitytrace.market.providers.twelve_data._WINDOW_OVERLAP_DAYS", 2)


def _overlap_handler(*, conflict: bool = False, shared: str = "2023-01-05"):
    call_n = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_n["n"] += 1
        q = parse_qs(urlparse(str(request.url)).query)
        start = q["start_date"][0]
        end = q["end_date"][0]
        close = "10" if (not conflict or call_n["n"] == 1) else "99"
        # Always include the shared overlap date so adjacent windows collide,
        # even when calendar-window math would otherwise omit it.
        days = [d for d in sorted({start, end, shared}) if start <= d <= end or d == shared]
        return httpx.Response(
            200,
            json={
                "meta": _meta(),
                "status": "ok",
                "values": _values(*days, close=close),
            },
        )

    return handler, call_n


def test_cross_window_identical_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_small_windows(monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    handler, _ = _overlap_handler(conflict=False)
    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 12), PriceAdjustmentMode.NONE
        )
    assert date(2023, 1, 5) in {b.trading_date for b in result.bars}
    assert result.duplicate_row_count >= 1
    assert date(2023, 1, 5) not in result.conflicting_duplicate_dates
    assert len(result.bars) == len({b.trading_date for b in result.bars})


def test_cross_window_conflicting_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_small_windows(monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    handler, _ = _overlap_handler(conflict=True)
    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 12), PriceAdjustmentMode.NONE
        )
    assert date(2023, 1, 5) in result.conflicting_duplicate_dates
    assert date(2023, 1, 5) not in {b.trading_date for b in result.bars}
    assert result.duplicate_row_count >= 2


def test_cross_window_three_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_small_windows(monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    handler, _ = _overlap_handler(conflict=False)
    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 18), PriceAdjustmentMode.NONE
        )
    assert result.duplicate_row_count >= 2
    assert sum(1 for b in result.bars if b.trading_date == date(2023, 1, 5)) == 1


def test_cross_window_conflict_then_another_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_small_windows(monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    handler, _ = _overlap_handler(conflict=True)
    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 18), PriceAdjustmentMode.NONE
        )
    assert date(2023, 1, 5) in result.conflicting_duplicate_dates
    assert result.duplicate_row_count >= 3


@pytest.mark.parametrize(
    "mode",
    [PriceAdjustmentMode.NONE, PriceAdjustmentMode.ALL],
)
def test_cross_window_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: PriceAdjustmentMode
) -> None:
    _patch_small_windows(monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    handler, _ = _overlap_handler(conflict=True)
    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        result = provider.fetch_daily_bars("AAPL", date(2023, 1, 1), date(2023, 1, 12), mode)
    assert result.adjustment_mode is mode
    assert date(2023, 1, 5) in result.conflicting_duplicate_dates


def test_cross_window_warning_and_count_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_small_windows(monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    handler, _ = _overlap_handler(conflict=True)
    with TwelveDataProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        response = provider.fetch_daily_bars(
            "AAPL", date(2023, 1, 1), date(2023, 1, 12), PriceAdjustmentMode.NONE
        )

    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = response
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 12),
            modes=[PriceAdjustmentMode.NONE],
        )
    assert any(w.startswith("conflicting_duplicate_dates:") for w in result.warnings)
    assert (
        result.raw_row_count
        == result.inserted_row_count
        + result.updated_row_count
        + result.unchanged_row_count
        + result.rejected_row_count
    )
    accepted = len(response.bars)
    assert (
        accepted + response.malformed_row_count + response.duplicate_row_count
        == accepted + response.duplicate_row_count
    )


def test_orphan_symbol_mapping_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        repo = MarketRepository(conn)
        with pytest.raises(ValueError, match="Referential integrity"):
            repo.upsert_symbol_mapping(
                MarketSymbolMapping(
                    instrument_id="instr_does_not_exist",
                    provider=MarketDataProviderName.TWELVE_DATA,
                    provider_symbol="AAPL",
                    is_primary=True,
                )
            )


def test_orphan_price_bar_fails_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    now = datetime(2023, 1, 4, 21, 15, tzinfo=UTC)
    with db.session() as conn:
        repo = MarketRepository(conn)
        good = repo.get_or_create_instrument("AAPL")
        good_bar = DailyPriceBar(
            instrument_id=good.instrument_id,
            provider=MarketDataProviderName.TWELVE_DATA,
            trading_date=date(2023, 1, 3),
            adjustment_mode=PriceAdjustmentMode.NONE,
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=10,
            available_at=now,
            fetched_at=now,
        )
        orphan = good_bar.model_copy(update={"instrument_id": "instr_missing"})
        with pytest.raises(ValueError, match="Referential integrity"):
            repo.upsert_price_bars_atomic([good_bar, orphan])
        assert (
            repo.get_price_bars(good.instrument_id, adjustment_mode=PriceAdjustmentMode.NONE) == []
        )


def test_orphan_market_data_run_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        repo = MarketRepository(conn)
        with pytest.raises(ValueError, match="Referential integrity"):
            repo.create_market_data_run(
                provider=MarketDataProviderName.TWELVE_DATA,
                instrument_id="instr_missing",
                requested_start_date=date(2023, 1, 1),
                requested_end_date=date(2023, 1, 5),
                adjustment_modes=[PriceAdjustmentMode.NONE],
            )


def test_tokyo_jpy_instrument_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"],
        mode=PriceAdjustmentMode.NONE,
        currency="JPY",
        tz="Asia/Tokyo",
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "7203",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        instrument = MarketRepository(conn).get_instrument(result.instrument_id)
        assert instrument is not None
        assert instrument.currency == "JPY"
        assert instrument.exchange_timezone == "Asia/Tokyo"
        assert instrument.market_metadata_confirmed is True


def test_utc_usd_instrument_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"],
        mode=PriceAdjustmentMode.NONE,
        currency="USD",
        tz="UTC",
    )
    with db.session() as conn:
        result = MarketDataService(conn, settings, provider=fake).ingest(
            "CRYPTOX",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        instrument = MarketRepository(conn).get_instrument(result.instrument_id)
        assert instrument is not None
        assert instrument.currency == "USD"
        assert instrument.exchange_timezone == "UTC"
        assert instrument.market_metadata_confirmed is True


def test_repeated_ingestion_matching_metadata(
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
        assert first.instrument_id == second.instrument_id
        instrument = MarketRepository(conn).get_instrument(first.instrument_id)
        assert instrument is not None
        assert instrument.currency == "USD"
        assert instrument.exchange_timezone == "America/New_York"
        assert instrument.market_metadata_confirmed is True


def test_conflicting_later_currency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE, currency="USD", tz="America/New_York"
    )
    with db.session() as conn:
        service = MarketDataService(conn, settings, provider=fake)
        service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
            ["2023-01-04"],
            mode=PriceAdjustmentMode.NONE,
            currency="EUR",
            tz="America/New_York",
        )
        result = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.status is MarketDataRunStatus.FAILED
        assert result.error_summary is not None
        assert "confirmed market metadata" in result.error_summary
        instrument = MarketRepository(conn).get_instrument_by_symbol("AAPL")
        assert instrument is not None
        assert instrument.currency == "USD"


def test_conflicting_later_timezone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        instrument_id = first.instrument_id
        fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
            ["2023-01-04"],
            mode=PriceAdjustmentMode.NONE,
            currency="USD",
            tz="Asia/Tokyo",
        )
        result = service.ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert result.status is MarketDataRunStatus.FAILED
        instrument = MarketRepository(conn).get_instrument(instrument_id)
        assert instrument is not None
        assert instrument.exchange_timezone == "America/New_York"
        assert instrument.instrument_id == instrument_id
        bars = MarketRepository(conn).get_price_bars(
            instrument_id, adjustment_mode=PriceAdjustmentMode.NONE
        )
        assert len(bars) == 1
        assert bars[0].instrument_id == instrument_id


def test_existing_instrument_id_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    fake = _FakeProvider()
    fake.responses[PriceAdjustmentMode.NONE] = _bars_response(
        ["2023-01-03"], mode=PriceAdjustmentMode.NONE, currency="JPY", tz="Asia/Tokyo"
    )
    with db.session() as conn:
        service = MarketDataService(conn, settings, provider=fake)
        first = service.ingest(
            "7203",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        second = service.ingest(
            "7203",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            modes=[PriceAdjustmentMode.NONE],
        )
        assert first.instrument_id == second.instrument_id
