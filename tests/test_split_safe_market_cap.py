"""Issue #22: split-safe point-in-time market capitalization regressions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from equitytrace.config import Settings, clear_settings_cache
from equitytrace.database import Database, initialize_database
from equitytrace.factors.engine import FactorEngine
from equitytrace.market.availability import bar_available_at
from equitytrace.market.market_cap import MarketCapFrequency, MarketCapService
from equitytrace.market.models import (
    DailyPriceBar,
    MarketDataProviderName,
    MarketSymbolMapping,
    PriceAdjustmentMode,
    SplitCoverageStatus,
)
from equitytrace.market.providers.twelve_data import TwelveDataProvider
from equitytrace.market.service import MarketDataService
from equitytrace.market.splits import (
    SPLIT_HISTORY_NON_VINTAGE_WARNING,
    SplitRatioError,
    parse_split_ratio,
)
from equitytrace.models import FinancialFact, Issuer, Security
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.issuers import IssuersRepository
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository
from helpers.split_coverage import seed_split_event, seed_verified_split_coverage


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "split.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_MARKET_DATA_CACHE_DIR", str(tmp_path / "mcache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _seed_issuer(conn: object, *, ticker: str = "AAA", cik: str = "0001000001") -> None:
    IssuersRepository(conn).upsert(Issuer(cik=cik, legal_name=f"{ticker} Corp"))  # type: ignore[arg-type]
    SecuritiesRepository(conn).upsert_many(  # type: ignore[arg-type]
        [Security(ticker=ticker, cik=cik, is_primary=True)]
    )


def _seed_shares(
    conn: object,
    *,
    cik: str = "0001000001",
    value: float,
    end: date,
    available: datetime,
    accession: str = "0001000001-23-000001",
) -> None:
    FactsRepository(conn).upsert_many(  # type: ignore[arg-type]
        [
            FinancialFact(
                cik=cik,
                taxonomy="us-gaap",
                concept="CommonStockSharesOutstanding",
                unit="shares",
                value=value,
                end_date=end,
                available_at=available,
                accession_number=accession,
                form="10-K",
                filing_date=end,
            )
        ]
    )


def _seed_raw_price(
    conn: object,
    *,
    ticker: str,
    cik: str,
    trading_date: date,
    close: Decimal,
) -> str:
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    instrument = repo.get_or_create_instrument(ticker, issuer_cik=cik, security_ticker=ticker)
    repo.upsert_price_bars(
        [
            DailyPriceBar(
                instrument_id=instrument.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                trading_date=trading_date,
                adjustment_mode=PriceAdjustmentMode.NONE,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=10,
                available_at=bar_available_at(trading_date, "America/New_York"),
                fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        ]
    )
    return instrument.instrument_id


def test_parse_split_ratio_four_for_one_and_reverse() -> None:
    four = parse_split_ratio(
        from_factor=4,
        to_factor=1,
        source_ratio=Decimal("0.25"),
        description="4-for-1 split",
    )
    assert four.share_multiplier == Decimal("4")
    reverse = parse_split_ratio(
        from_factor=1,
        to_factor=10,
        source_ratio=Decimal("10"),
        description="1-for-10 split",
    )
    assert reverse.share_multiplier == Decimal("0.1")
    with pytest.raises(SplitRatioError):
        parse_split_ratio(from_factor=4, to_factor=1, source_ratio=Decimal("4"))
    with pytest.raises(SplitRatioError):
        parse_split_ratio(
            from_factor=4,
            to_factor=1,
            source_ratio=Decimal("0.25"),
            description="2-for-1 split",
        )


@pytest.mark.parametrize("field", ["from_factor", "to_factor", "source_ratio"])
@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity", float("nan"), float("inf")])
def test_nonfinite_split_factor_rejected_as_validation_error(field: str, bad: object) -> None:
    # A malformed provider row must fail closed, not raise decimal.InvalidOperation
    # or propagate infinite market-cap multipliers into persisted research results.
    values: dict[str, object] = {
        "from_factor": 4,
        "to_factor": 1,
        "source_ratio": "0.25",
    }
    values[field] = bad
    with pytest.raises(SplitRatioError, match="finite and positive"):
        parse_split_ratio(**values)


def test_four_for_one_split_before_price_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("50"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 2, 10),
            provider_symbol="AAA",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 1),
            from_factor=4,
            to_factor=1,
            source_ratio="0.25",
            description="4-for-1 split",
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert result.is_available
    assert result.raw_close == Decimal("50")
    assert result.shares_outstanding == Decimal("1000000")
    assert result.reconciled_shares_outstanding == Decimal("4000000")
    assert result.split_share_multiplier == Decimal("4")
    assert result.market_cap == Decimal("200000000")
    assert result.split_event_ids
    assert SPLIT_HISTORY_NON_VINTAGE_WARNING in result.warnings


def test_split_effective_exactly_on_price_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    price_day = date(2023, 2, 10)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn, ticker="AAA", cik="0001000001", trading_date=price_day, close=Decimal("25")
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=price_day,
            provider_symbol="AAA",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=price_day,
            from_factor=4,
            to_factor=1,
            description="4-for-1 split",
            source_ratio="0.25",
        )
        result = MarketCapService(conn).get_market_cap("AAA", price_day)
    assert result.market_cap == Decimal("100000000")
    assert result.split_share_multiplier == Decimal("4")


def test_reverse_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=10_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 3, 1),
            close=Decimal("100"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 3, 1),
            provider_symbol="AAA",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 15),
            from_factor=1,
            to_factor=10,
            description="1-for-10 split",
            source_ratio="10",
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 3, 1))
    assert result.reconciled_shares_outstanding == Decimal("1000000")
    assert result.market_cap == Decimal("100000000")


def test_compounded_multiple_splits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 6, 1),
            close=Decimal("10"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 6, 1),
            provider_symbol="AAA",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 1),
            from_factor=2,
            to_factor=1,
            description="2-for-1 split",
            source_ratio="0.5",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 4, 1),
            from_factor=3,
            to_factor=1,
            description="3-for-1 split",
            source_ratio=Decimal("1") / Decimal("3"),
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 6, 1))
    assert result.split_share_multiplier == Decimal("6")
    assert result.reconciled_shares_outstanding == Decimal("6000000")
    assert result.market_cap == Decimal("60000000")
    assert len(result.split_event_ids) == 2


def test_post_split_shares_fact_no_double_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=4_000_000,
            end=date(2023, 2, 5),
            available=datetime(2023, 2, 6, tzinfo=UTC),
            accession="0001000001-23-000002",
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("50"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 2, 10),
            provider_symbol="AAA",
        )
        # Split before the post-split SEC fact date must not re-apply.
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 1),
            from_factor=4,
            to_factor=1,
            description="4-for-1 split",
            source_ratio="0.25",
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert result.shares_outstanding == Decimal("4000000")
    assert result.split_share_multiplier == Decimal("1")
    assert result.market_cap == Decimal("200000000")
    assert result.split_event_ids == ()


def test_genuine_no_split_with_verified_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("100"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 2, 10),
            provider_symbol="AAA",
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert result.market_cap == Decimal("100000000")
    assert result.split_share_multiplier == Decimal("1")
    assert result.split_event_ids == ()
    assert SPLIT_HISTORY_NON_VINTAGE_WARNING in result.warnings


def test_unknown_coverage_and_gap_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("100"),
        )
        missing = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
        assert missing.unavailable_reason == "split_coverage_unavailable"
        assert missing.market_cap is None
        assert missing.raw_close == Decimal("100")
        assert missing.shares_outstanding == Decimal("1000000")

        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 15),
            coverage_end=date(2023, 2, 10),
            provider_symbol="AAA",
            completeness_basis="synthetic_starts_late",
        )
        late = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
        assert late.unavailable_reason == "split_coverage_starts_too_late"

        # Replace with early-ending coverage.
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 2, 1),
            provider_symbol="AAA",
            completeness_basis="synthetic_ends_early",
        )
        early = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
        assert early.unavailable_reason in {
            "split_coverage_ends_before_price_date",
            "split_coverage_unavailable",
        }

        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 2, 10),
            provider_symbol="AAA",
            status=SplitCoverageStatus.UNKNOWN,
            completeness_basis="synthetic_unknown",
        )
        unknown = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
        assert unknown.unavailable_reason in {
            "split_coverage_unknown",
            "split_coverage_unavailable",
        }


def test_split_after_price_date_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("100"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 3, 1),
            provider_symbol="AAA",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 20),
            from_factor=4,
            to_factor=1,
            description="4-for-1 split",
            source_ratio="0.25",
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert result.market_cap == Decimal("100000000")
    assert result.split_event_ids == ()


def test_event_not_eligible_at_as_of(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    price_day = date(2023, 2, 10)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn, ticker="AAA", cik="0001000001", trading_date=price_day, close=Decimal("25")
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=price_day,
            provider_symbol="AAA",
        )
        future_available = bar_available_at(price_day, "America/New_York") + timedelta(days=1)
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 1),
            from_factor=4,
            to_factor=1,
            description="4-for-1 split",
            source_ratio="0.25",
            available_at=future_available,
        )
        early = MarketCapService(conn).get_market_cap(
            "AAA",
            price_day,
            as_of=bar_available_at(price_day, "America/New_York"),
        )
        late = MarketCapService(conn).get_market_cap(
            "AAA",
            price_day,
            as_of=future_available,
        )
    # Ineligible effective split is not "no split"; fail closed until eligible.
    assert early.market_cap is None
    assert early.unavailable_reason == "split_event_unavailable_at_knowledge_time"
    # Understated pre-split cap must never become available early.
    assert early.market_cap != Decimal("25000000")
    assert late.market_cap == Decimal("100000000")
    assert late.split_share_multiplier == Decimal("4")
    assert SPLIT_HISTORY_NON_VINTAGE_WARNING in late.warnings


def test_conflicting_split_events_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("50"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 2, 10),
            provider_symbol="AAA",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 1),
            from_factor=4,
            to_factor=1,
            description="4-for-1 split",
            source_ratio="0.25",
        )
        # Conflict: same effective date, different multiplier → different event_id.
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=date(2023, 2, 1),
            from_factor=2,
            to_factor=1,
            description="2-for-1 split",
            source_ratio="0.5",
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert result.unavailable_reason == "conflicting_split_events"
    assert result.market_cap is None


def test_share_class_ambiguity_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        IssuersRepository(conn).upsert(Issuer(cik="0001000001", legal_name="Multi"))
        SecuritiesRepository(conn).upsert_many(
            [
                Security(ticker="GOOGL", cik="0001000001", is_primary=True),
                Security(ticker="GOOG", cik="0001000001", is_primary=False),
            ]
        )
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="GOOGL",
            cik="0001000001",
            trading_date=date(2023, 1, 3),
            close=Decimal("100"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 1, 3),
            provider_symbol="GOOGL",
        )
        result = MarketCapService(conn).get_market_cap("GOOGL", date(2023, 1, 3))
    assert result.unavailable_reason == "multi_class_issuer_ambiguous"


def test_provider_splits_ingest_empty_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    empty_payload = {
        "meta": {
            "symbol": "AAA",
            "currency": "USD",
            "exchange": "NASDAQ",
            "exchange_timezone": "America/New_York",
        },
        "splits": [],
    }
    event_payload = {
        "meta": {
            "symbol": "AAA",
            "currency": "USD",
            "exchange": "NASDAQ",
            "exchange_timezone": "America/New_York",
        },
        "splits": [
            {
                "date": "2023-02-01",
                "description": "4-for-1 split",
                "ratio": 0.25,
                "from_factor": 4,
                "to_factor": 1,
            }
        ],
    }
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "apikey" not in str(request.url)
        assert request.headers.get("Authorization", "").startswith("apikey ")
        assert request.url.path.endswith("/splits")
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json=empty_payload)
        return httpx.Response(200, json=event_payload)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        service = MarketDataService(conn, settings, provider=provider)
        first = service.ingest_splits("AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1))
        second = service.ingest_splits(
            "AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1)
        )
        third = service.ingest_splits("AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1))
    assert first.status.value == "success"
    assert first.event_count == 0
    assert first.coverage_status is SplitCoverageStatus.VERIFIED
    assert second.event_count == 1
    assert third.unchanged_event_count + third.updated_event_count + third.inserted_event_count >= 0


def test_provider_splits_plan_denied_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "code": 403, "message": "This plan is not available"},
        )

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        result = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1)
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAA")
        assert instrument is not None
        coverage = repo.get_verified_split_coverage(
            instrument.instrument_id,
            shares_fact_date=date(2023, 1, 1),
            price_date=date(2023, 2, 1),
        )
    assert result.status.value == "failed"
    assert coverage is None or coverage.status is not SplitCoverageStatus.VERIFIED


def test_market_cap_series_propagates_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        repo = MarketRepository(conn)
        instrument = repo.get_or_create_instrument(
            "AAA", issuer_cik="0001000001", security_ticker="AAA"
        )
        for d, close in (
            (date(2023, 1, 10), "100"),
            (date(2023, 2, 10), "50"),
        ):
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
                        fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
                    )
                ]
            )
        points = MarketCapService(conn).get_market_cap_series(
            "AAA",
            date(2023, 1, 1),
            date(2023, 2, 28),
            frequency=MarketCapFrequency.MONTH_END,
        )
    assert points
    assert all(not p.result.is_available for p in points)
    assert all(p.result.unavailable_reason == "split_coverage_unavailable" for p in points)


def test_valuation_factors_propagate_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    as_of = datetime(2023, 2, 10, 23, 0, tzinfo=UTC)
    with db.session() as conn:
        _seed_issuer(conn)
        # Minimal FY facts for valuation factors.
        FactsRepository(conn).upsert_many(
            [
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="NetCashProvidedByUsedInOperatingActivities",
                    unit="USD",
                    value=100.0,
                    end_date=date(2022, 12, 31),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="0001000001-23-000010",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                    fiscal_year=2022,
                    fiscal_period="FY",
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="PaymentsToAcquirePropertyPlantAndEquipment",
                    unit="USD",
                    value=10.0,
                    end_date=date(2022, 12, 31),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="0001000001-23-000010",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                    fiscal_year=2022,
                    fiscal_period="FY",
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="NetIncomeLoss",
                    unit="USD",
                    value=50.0,
                    end_date=date(2022, 12, 31),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="0001000001-23-000010",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                    fiscal_year=2022,
                    fiscal_period="FY",
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="Revenues",
                    unit="USD",
                    value=200.0,
                    end_date=date(2022, 12, 31),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="0001000001-23-000010",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                    fiscal_year=2022,
                    fiscal_period="FY",
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="StockholdersEquity",
                    unit="USD",
                    value=80.0,
                    end_date=date(2022, 12, 31),
                    available_at=datetime(2023, 1, 1, tzinfo=UTC),
                    accession_number="0001000001-23-000010",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                    fiscal_year=2022,
                    fiscal_period="FY",
                ),
                FinancialFact(
                    cik="0001000001",
                    taxonomy="us-gaap",
                    concept="CommonStockSharesOutstanding",
                    unit="shares",
                    value=1_000_000,
                    end_date=date(2023, 1, 1),
                    available_at=datetime(2023, 1, 2, tzinfo=UTC),
                    accession_number="0001000001-23-000001",
                    form="10-K",
                    filing_date=date(2023, 1, 1),
                ),
            ]
        )
        _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("100"),
        )
        engine = FactorEngine(conn)
        for factor in (
            "fcf_yield",
            "price_to_earnings",
            "price_to_sales",
            "price_to_book",
        ):
            result = engine.calculate("AAA", factor, "FY2022", as_of=as_of, persist=False)
            assert not result.valid
            assert result.unavailable_reason is not None
            assert "split_coverage_unavailable" in result.unavailable_reason

        ranking = FactorEngine(conn).rank(
            ["AAA"],
            "fcf_yield",
            "FY2022",
            as_of=as_of,
        )
        assert ranking.valid_count == 0
        assert ranking.excluded


def test_malformed_provider_rows_never_verify_no_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 200 with invalid factors must not certify a no-split interval."""
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    payload = {
        "meta": {
            "symbol": "AAA",
            "exchange_timezone": "America/New_York",
        },
        "splits": [
            {
                "date": "2023-02-01",
                "description": "broken split",
                "ratio": 0.25,
                # Missing from_factor / to_factor → malformed
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/splits")
        return httpx.Response(200, json=payload)

    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("25"),
        )
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        ingest = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1)
        )
        cap = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))

    assert ingest.coverage_status is SplitCoverageStatus.ERROR
    assert ingest.status.value == "failed"
    assert ingest.event_count == 0
    assert cap.market_cap is None
    # Understated no-split cap (25 * 1e6) must not become available.
    assert cap.market_cap != Decimal("25000000")
    assert cap.unavailable_reason in {
        "split_coverage_error",
        "split_coverage_unavailable",
        "split_coverage_unknown",
    }


def test_mixed_valid_and_invalid_split_rows_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    payload = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [
            {
                "date": "2023-02-01",
                "description": "4-for-1 split",
                "ratio": 0.25,
                "from_factor": 4,
                "to_factor": 1,
            },
            {
                "date": "2023-02-15",
                "description": "bad row",
                "ratio": 0.5,
                "from_factor": 0,
                "to_factor": 1,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        result = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1)
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAA")
        assert instrument is not None
        events = repo.list_stock_split_events(instrument.instrument_id)
        coverage = repo.get_verified_split_coverage(
            instrument.instrument_id,
            shares_fact_date=date(2023, 1, 1),
            price_date=date(2023, 2, 20),
            provider_symbol="AAA",
        )
    assert result.coverage_status is SplitCoverageStatus.ERROR
    assert result.event_count == 0
    assert events == []
    assert coverage is None or coverage.status is not SplitCoverageStatus.VERIFIED


def test_conflicting_provider_split_rows_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    payload = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [
            {
                "date": "2023-02-01",
                "description": "4-for-1 split",
                "ratio": 0.25,
                "from_factor": 4,
                "to_factor": 1,
            },
            {
                "date": "2023-02-01",
                "description": "2-for-1 split",
                "ratio": 0.5,
                "from_factor": 2,
                "to_factor": 1,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        result = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1)
        )
    assert result.coverage_status is SplitCoverageStatus.ERROR
    assert result.event_count == 0
    assert result.error_summary is not None
    assert "incomplete" in result.error_summary


def test_verified_refresh_removes_stale_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with_event = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [
            {
                "date": "2023-02-01",
                "description": "4-for-1 split",
                "ratio": 0.25,
                "from_factor": 4,
                "to_factor": 1,
            }
        ],
    }
    empty = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(200, json=with_event if state["n"] == 1 else empty)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        service = MarketDataService(conn, settings, provider=provider)
        first = service.ingest_splits("AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1))
        second = service.ingest_splits(
            "AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1)
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAA")
        assert instrument is not None
        events = repo.list_stock_split_events(
            instrument.instrument_id,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 3, 1),
            provider_symbol="AAA",
        )
        coverage = repo.get_verified_split_coverage(
            instrument.instrument_id,
            shares_fact_date=date(2023, 1, 1),
            price_date=date(2023, 2, 10),
            provider_symbol="AAA",
        )
    assert first.event_count == 1
    assert second.event_count == 0
    assert second.coverage_status is SplitCoverageStatus.VERIFIED
    assert events == []
    assert coverage is not None and coverage.status is SplitCoverageStatus.VERIFIED


def test_failed_refresh_demotes_verified_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    good = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }
    bad = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [{"date": "2023-02-01", "ratio": 0.25}],
    }
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(200, json=good if state["n"] == 1 else bad)

    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("100"),
        )
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        service = MarketDataService(conn, settings, provider=provider)
        first = service.ingest_splits("AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1))
        before = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
        second = service.ingest_splits(
            "AAA", start_date=date(2023, 1, 1), end_date=date(2023, 3, 1)
        )
        after = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert first.coverage_status is SplitCoverageStatus.VERIFIED
    assert before.market_cap == Decimal("100000000")
    assert second.coverage_status is SplitCoverageStatus.ERROR
    assert after.market_cap is None
    assert after.unavailable_reason in {
        "split_coverage_error",
        "split_coverage_unavailable",
        "split_coverage_unknown",
    }


def test_mismatched_provider_symbol_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=date(2023, 2, 10),
            close=Decimal("100"),
        )
        repo = MarketRepository(conn)
        repo.upsert_symbol_mapping(
            MarketSymbolMapping(
                instrument_id=instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                provider_symbol="AAA.US",
                is_primary=True,
            )
        )
        # Coverage/events bound to a different provider symbol than the mapping.
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 2, 10),
            provider_symbol="AAA",
        )
        result = MarketCapService(conn).get_market_cap("AAA", date(2023, 2, 10))
    assert result.market_cap is None
    assert result.unavailable_reason in {
        "split_coverage_unavailable",
        "split_coverage_unknown",
        "split_provider_symbol_ambiguous",
    }


def test_out_of_certifiable_depth_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    payload = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        result = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA",
            start_date=date(2000, 1, 1),
            end_date=date(2023, 3, 1),
        )
    assert result.coverage_status is SplitCoverageStatus.UNKNOWN
    assert result.status.value == "partial"
    assert result.error_summary is not None
    assert "depth_unknown" in result.error_summary


def _fixed_retrieval_2026(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin provider retrieval provenance to mid-2026 for depth regressions."""
    fixed = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    def _now() -> datetime:
        return fixed

    monkeypatch.setattr("equitytrace.market.providers.twelve_data.utc_now", _now)
    monkeypatch.setattr("equitytrace.market.service.utc_now", _now)
    return fixed


def test_past_window_outside_provider_retrieval_floor_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case A: short span still fails when start is before retrieval-10y."""
    settings = _settings(tmp_path, monkeypatch)
    _fixed_retrieval_2026(monkeypatch)
    db = initialize_database(settings.database_path)
    empty = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }
    with_event = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [
            {
                "date": "2015-06-01",
                "description": "2-for-1 split",
                "ratio": 0.5,
                "from_factor": 2,
                "to_factor": 1,
            }
        ],
    }
    for payload in (empty, with_event):

        def handler(request: httpx.Request, body: dict = payload) -> httpx.Response:
            return httpx.Response(200, json=body)

        with db.session() as conn:
            provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
            result = MarketDataService(conn, settings, provider=provider).ingest_splits(
                "AAA",
                start_date=date(2012, 1, 1),
                end_date=date(2020, 12, 31),
            )
        # Floor from 2026-06-15 is 2016-06-15; 2012 start is outside.
        assert result.coverage_status is SplitCoverageStatus.UNKNOWN
        assert result.status.value == "partial"
        assert result.error_summary is not None
        assert "depth_unknown" in result.error_summary


def test_supported_window_within_provider_retrieval_floor_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case B: 2019-2023 is inside a 2026 retrieval-10y floor when complete."""
    settings = _settings(tmp_path, monkeypatch)
    _fixed_retrieval_2026(monkeypatch)
    db = initialize_database(settings.database_path)
    empty = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }
    with_event = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [
            {
                "date": "2020-08-31",
                "description": "4-for-1 split",
                "ratio": 0.25,
                "from_factor": 4,
                "to_factor": 1,
            }
        ],
    }
    for payload, expected_events in ((empty, 0), (with_event, 1)):

        def handler(request: httpx.Request, body: dict = payload) -> httpx.Response:
            return httpx.Response(200, json=body)

        with db.session() as conn:
            provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
            result = MarketDataService(conn, settings, provider=provider).ingest_splits(
                "AAA",
                start_date=date(2019, 1, 1),
                end_date=date(2023, 12, 31),
            )
        assert result.coverage_status is SplitCoverageStatus.VERIFIED
        assert result.status.value == "success"
        assert result.event_count == expected_events


def test_future_request_end_does_not_extend_history_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case C: future end_date cannot invent post-retrieval coverage."""
    settings = _settings(tmp_path, monkeypatch)
    fixed = _fixed_retrieval_2026(monkeypatch)
    db = initialize_database(settings.database_path)
    payload = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        result = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA",
            start_date=date(2019, 1, 1),
            end_date=date(2035, 12, 31),
        )
    assert result.coverage_status is SplitCoverageStatus.UNKNOWN
    assert result.error_summary is not None
    assert "future_end" in result.error_summary
    # Floor remains retrieval-10y, not end_date-10y (2035 -> 2025).
    from equitytrace.market.providers.twelve_data import certifiable_splits_start

    assert certifiable_splits_start(fixed) == date(2016, 6, 15)
    assert certifiable_splits_start(date(2035, 12, 31)) == date(2025, 12, 31)


def test_cached_splits_without_retrieval_stamp_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case D: legacy cache bytes must not fabricate live retrieval provenance."""
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings = settings.model_copy(
        update={
            "database_path": tmp_path / "split.duckdb",
            "cache_dir": tmp_path / "cache",
            "market_data_cache_dir": tmp_path / "mcache",
            "enable_cache": True,
            "twelve_data_api_key": "test-key",
            "sec_email": "tests@example.com",
        }
    )
    _fixed_retrieval_2026(monkeypatch)
    db = initialize_database(settings.database_path)
    legacy_payload = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }

    # Pre-seed a raw (non-envelope) cache entry as older builds wrote.
    def _noop_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    provider_for_path = TwelveDataProvider(settings, transport=httpx.MockTransport(_noop_handler))
    cache_params = {
        "endpoint": "splits",
        "symbol": "AAA",
        "start_date": "2019-01-01",
        "end_date": "2023-12-31",
    }
    provider_for_path._write_cache(cache_params, legacy_payload)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=legacy_payload)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        result = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA",
            start_date=date(2019, 1, 1),
            end_date=date(2023, 12, 31),
        )
    # Cache hit must not invent a new retrieval timestamp for certification.
    assert calls["n"] == 0
    assert result.coverage_status is SplitCoverageStatus.UNKNOWN
    assert result.error_summary is not None
    assert "provenance_untrusted" in result.error_summary


def test_cached_splits_with_stamped_retrieval_may_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Envelope-stamped cache preserves original retrieval for depth checks."""
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings = settings.model_copy(
        update={
            "database_path": tmp_path / "split.duckdb",
            "cache_dir": tmp_path / "cache",
            "market_data_cache_dir": tmp_path / "mcache",
            "enable_cache": True,
            "twelve_data_api_key": "test-key",
            "sec_email": "tests@example.com",
        }
    )
    fixed = _fixed_retrieval_2026(monkeypatch)
    db = initialize_database(settings.database_path)
    payload = {
        "meta": {"symbol": "AAA", "exchange_timezone": "America/New_York"},
        "splits": [],
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=httpx.MockTransport(handler))
        first = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA",
            start_date=date(2019, 1, 1),
            end_date=date(2023, 12, 31),
        )
        # Advance wall clock; stamped cache provenance must stay at `fixed`.
        later = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
        monkeypatch.setattr("equitytrace.market.providers.twelve_data.utc_now", lambda: later)
        monkeypatch.setattr("equitytrace.market.service.utc_now", lambda: later)
        second = MarketDataService(conn, settings, provider=provider).ingest_splits(
            "AAA",
            start_date=date(2019, 1, 1),
            end_date=date(2023, 12, 31),
        )
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol("AAA")
        assert instrument is not None
        coverage = repo.get_verified_split_coverage(
            instrument.instrument_id,
            shares_fact_date=date(2019, 1, 1),
            price_date=date(2023, 12, 31),
            provider_symbol="AAA",
        )
    assert first.coverage_status is SplitCoverageStatus.VERIFIED
    assert second.coverage_status is SplitCoverageStatus.VERIFIED
    assert calls["n"] == 1
    assert coverage is not None
    assert coverage.retrieved_at.astimezone(UTC) == fixed


def test_effective_date_boundary_inclusive_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Split on price_date is in (fact, price]; day-after is outside the gap."""
    settings = _settings(tmp_path, monkeypatch)
    db = initialize_database(settings.database_path)
    price_day = date(2023, 2, 1)
    with db.session() as conn:
        _seed_issuer(conn)
        _seed_shares(
            conn,
            value=1_000_000,
            end=date(2023, 1, 1),
            available=datetime(2023, 1, 2, tzinfo=UTC),
        )
        instrument_id = _seed_raw_price(
            conn,
            ticker="AAA",
            cik="0001000001",
            trading_date=price_day,
            close=Decimal("25"),
        )
        seed_verified_split_coverage(
            conn,
            instrument_id=instrument_id,
            coverage_start=date(2023, 1, 1),
            coverage_end=price_day,
            provider_symbol="AAA",
        )
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=price_day,
            from_factor=4,
            to_factor=1,
            description="4-for-1 split",
            source_ratio="0.25",
        )
        on_price_day = MarketCapService(conn).get_market_cap("AAA", price_day)
        # Event strictly after price_date must not adjust the share basis.
        seed_split_event(
            conn,
            instrument_id=instrument_id,
            provider_symbol="AAA",
            effective_date=price_day + timedelta(days=1),
            from_factor=2,
            to_factor=1,
            description="2-for-1 split",
            source_ratio="0.5",
        )
        still = MarketCapService(conn).get_market_cap("AAA", price_day)
    assert on_price_day.market_cap == Decimal("100000000")
    assert on_price_day.split_share_multiplier == Decimal("4")
    assert still.market_cap == Decimal("100000000")
    assert still.split_share_multiplier == Decimal("4")


def test_migration_adds_split_tables_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "mig.duckdb"
    db = initialize_database(path)
    initialize_database(path)
    with db.session() as conn:
        tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()]
    assert "stock_split_events" in tables
    assert "stock_split_coverage" in tables
    assert versions.count("1.0.0-a") == 1


def test_upgrade_from_v03c_adds_split_tables(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    legacy = Database(path)
    with legacy.session() as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                notes VARCHAR
            )
            """
        )
        conn.execute("INSERT INTO schema_migrations VALUES ('0.3.0-c', CURRENT_TIMESTAMP, 'v03c')")
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
                currency VARCHAR NOT NULL DEFAULT 'USD',
                exchange_timezone VARCHAR NOT NULL DEFAULT 'America/New_York',
                market_metadata_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    initialize_database(path)
    with Database(path).session() as conn:
        tables = {str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()}
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    assert "stock_split_events" in tables
    assert "stock_split_coverage" in tables
    assert "1.0.0-a" in versions
