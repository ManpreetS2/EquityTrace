"""Market-window analytics tests: momentum, volatility, beta, drawdown, PIT."""

from __future__ import annotations

import math
import statistics
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from equitytrace.database import Database
from equitytrace.market.analytics import (
    ADJUSTED_HISTORY_WARNING,
    REQUIRED_CLOSES,
    REQUIRED_RETURNS,
    BetaHasNoRankingDirection,
    MarketAnalyticsService,
)
from equitytrace.market.availability import bar_available_at
from equitytrace.market.models import (
    DailyPriceBar,
    MarketDataProviderName,
    PriceAdjustmentMode,
)
from equitytrace.repositories.market import MarketRepository

AS_OF = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)
FETCHED = datetime(2025, 1, 2, tzinfo=UTC)


def _weekdays_ending_on(end: date, count: int) -> list[date]:
    days: list[date] = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return list(reversed(days))


def _bar(
    instrument_id: str,
    trading_date: date,
    close: Decimal,
    *,
    mode: PriceAdjustmentMode = PriceAdjustmentMode.ALL,
    available_at: datetime | None = None,
) -> DailyPriceBar:
    known = available_at or bar_available_at(trading_date, "America/New_York")
    return DailyPriceBar(
        instrument_id=instrument_id,
        provider=MarketDataProviderName.TWELVE_DATA,
        trading_date=trading_date,
        adjustment_mode=mode,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        available_at=known,
        fetched_at=FETCHED,
    )


def _seed_symbol(
    conn: object,
    ticker: str,
    closes: list[tuple[date, Decimal]],
    *,
    also_raw: bool = True,
) -> str:
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    instrument = repo.get_or_create_instrument(ticker)
    bars = [_bar(instrument.instrument_id, day, close) for day, close in closes]
    if also_raw:
        bars.extend(
            [
                _bar(instrument.instrument_id, day, close * 2, mode=PriceAdjustmentMode.NONE)
                for day, close in closes
            ]
        )
    repo.upsert_price_bars(bars)
    return instrument.instrument_id


def test_momentum_12_1_formula_skips_recent_21(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[0] = Decimal("50")
    closes[-22] = Decimal("80")
    closes[-1] = Decimal("999")
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        result = MarketAnalyticsService(conn).calculate("AAA", "momentum_12_1", AS_OF)
    assert result.valid
    assert result.value == pytest.approx(80 / 50 - 1)
    assert result.window_start == dates[0]
    assert result.window_end == dates[-22]
    assert result.observation_count == REQUIRED_CLOSES
    assert result.adjustment_mode == PriceAdjustmentMode.ALL
    assert ADJUSTED_HISTORY_WARNING in result.warnings


def test_momentum_uses_adjusted_not_raw(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[0] = Decimal("50")
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        result = MarketAnalyticsService(conn).calculate("AAA", "momentum", AS_OF)
    assert result.valid
    assert result.value == pytest.approx(100 / 50 - 1)


def test_momentum_insufficient_history(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES - 1)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10")) for d in dates])
        result = MarketAnalyticsService(conn).calculate("AAA", "momentum_12_1", AS_OF)
    assert result.valid is False
    assert result.unavailable_reason == "insufficient_history"


def test_volatility_sample_stdev_and_insufficient(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal(str(100 + i)) for i in range(REQUIRED_CLOSES)]
    expected_returns = [float(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes))]
    expected = statistics.stdev(expected_returns) * math.sqrt(252)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        result = MarketAnalyticsService(conn).calculate("AAA", "volatility_1y", AS_OF)
        short = MarketAnalyticsService(conn).calculate("BBB", "volatility_1y", AS_OF)
    assert result.valid
    assert result.value == pytest.approx(expected)
    assert result.ranking_direction == "lower_is_better"
    assert short.valid is False


def test_beta_known_ratio_default_spy_and_override(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    spy_rets = [0.01 * ((i % 5) - 2) for i in range(REQUIRED_RETURNS)]
    aaa_rets = [2.0 * r for r in spy_rets]
    qqq_rets = [0.5 * r for r in spy_rets]

    def _path(returns: list[float]) -> list[Decimal]:
        prices = [Decimal("100")]
        for ret in returns:
            prices.append(prices[-1] * (Decimal("1") + Decimal(str(ret))))
        return prices

    spy = _path(spy_rets)
    aaa = _path(aaa_rets)
    qqq = _path(qqq_rets)
    pairs = list(zip(dates, spy, aaa, qqq, strict=True))
    with db.session() as conn:
        _seed_symbol(conn, "SPY", [(d, s) for d, s, _, _ in pairs])
        _seed_symbol(conn, "AAA", [(d, a) for d, _, a, _ in pairs])
        _seed_symbol(conn, "QQQ", [(d, q) for d, _, _, q in pairs])
        svc = MarketAnalyticsService(conn)
        default = svc.calculate("AAA", "beta_1y", AS_OF)
        vs_qqq = svc.calculate("AAA", "beta", AS_OF, benchmark="QQQ")
    assert default.valid
    assert default.benchmark == "SPY"
    assert default.ranking_direction is None
    assert default.value == pytest.approx(2.0, rel=1e-9)
    assert vs_qqq.valid
    assert vs_qqq.benchmark == "QQQ"
    expected_qqq = statistics.covariance(aaa_rets, qqq_rets) / statistics.variance(qqq_rets)
    assert vs_qqq.value == pytest.approx(expected_qqq)


def test_beta_interior_gap_uses_matching_intervals(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_RETURNS + 2)
    gap = dates[len(dates) // 2]
    after_gap = dates[dates.index(gap) + 1]
    spy_closes = {d: Decimal("100") for d in dates}
    aaa_closes = {d: Decimal("100") for d in dates}
    spy_closes[gap] = Decimal("150")
    spy_closes[after_gap] = Decimal("200")
    aaa_closes[after_gap] = Decimal("200")
    aaa_dates = [d for d in dates if d != gap]
    with db.session() as conn:
        _seed_symbol(conn, "SPY", [(d, spy_closes[d]) for d in dates])
        _seed_symbol(conn, "AAA", [(d, aaa_closes[d]) for d in aaa_dates])
        result = MarketAnalyticsService(conn).calculate("AAA", "beta_1y", AS_OF)

    common = [d for d in dates if d != gap]
    common = common[-(REQUIRED_RETURNS + 1) :]
    asset_prices = [float(aaa_closes[d]) for d in common]
    bench_prices = [float(spy_closes[d]) for d in common]
    asset_rets = [asset_prices[i] / asset_prices[i - 1] - 1.0 for i in range(1, len(asset_prices))]
    bench_rets = [bench_prices[i] / bench_prices[i - 1] - 1.0 for i in range(1, len(bench_prices))]
    expected = statistics.covariance(asset_rets, bench_rets) / statistics.variance(bench_rets)

    # Old join-by-end-date would pair AAA's Mon→Wed move with SPY's Tue→Wed move.
    old_asset: dict[date, float] = {}
    ordered_aaa = aaa_dates
    for prev, cur in pairwise(ordered_aaa):
        old_asset[cur] = float(aaa_closes[cur] / aaa_closes[prev]) - 1.0
    old_bench: dict[date, float] = {}
    for prev, cur in pairwise(dates):
        old_bench[cur] = float(spy_closes[cur] / spy_closes[prev]) - 1.0
    old_common = sorted(set(old_asset) & set(old_bench))[-REQUIRED_RETURNS:]
    old_a = [old_asset[d] for d in old_common]
    old_b = [old_bench[d] for d in old_common]
    old_beta = statistics.covariance(old_a, old_b) / statistics.variance(old_b)

    assert result.valid
    assert result.value == pytest.approx(expected)
    assert result.value != pytest.approx(old_beta)
    assert result.window_start == common[0]
    assert result.window_end == common[-1]
    assert ADJUSTED_HISTORY_WARNING in result.warnings


def test_beta_inner_join_no_forward_fill(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10") + Decimal(i)) for i, d in enumerate(dates)])
        bench_dates = dates[:-1]
        _seed_symbol(
            conn,
            "SPY",
            [(d, Decimal("20") + Decimal(i)) for i, d in enumerate(bench_dates)],
        )
        result = MarketAnalyticsService(conn).calculate("AAA", "beta_1y", AS_OF)
    assert result.valid is False
    assert result.unavailable_reason == "insufficient_aligned_observations"


def test_beta_zero_variance_unavailable(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10") + Decimal(i)) for i, d in enumerate(dates)])
        _seed_symbol(conn, "SPY", [(d, Decimal("100")) for d in dates])
        result = MarketAnalyticsService(conn).calculate("AAA", "beta_1y", AS_OF)
    assert result.valid is False
    assert result.unavailable_reason == "zero_benchmark_variance"


def test_max_drawdown_known_trough(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[-1] = Decimal("50")
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        result = MarketAnalyticsService(conn).calculate("AAA", "max_drawdown_1y", AS_OF)
    assert result.valid
    assert result.value == pytest.approx(-0.5)
    assert result.value is not None and result.value <= 0
    assert result.ranking_direction == "higher_is_better"


def test_future_trading_date_excluded(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[0] = Decimal("50")
    with db.session() as conn:
        instrument_id = _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        future_day = AS_OF.date() + timedelta(days=2)
        while future_day.weekday() >= 5:
            future_day += timedelta(days=1)
        MarketRepository(conn).upsert_price_bars([_bar(instrument_id, future_day, Decimal("9"))])
        result = MarketAnalyticsService(conn).calculate("AAA", "momentum_12_1", AS_OF)
    assert result.valid
    assert result.value == pytest.approx(1.0)
    assert result.window_start == dates[0]
    assert result.last_trading_date != future_day


def test_future_known_bar_in_window_excluded(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[0] = Decimal("50")
    with db.session() as conn:
        instrument_id = _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        late = AS_OF + timedelta(days=5)
        MarketRepository(conn).upsert_price_bars(
            [_bar(instrument_id, dates[-1], Decimal("9"), available_at=late)]
        )
        result = MarketAnalyticsService(conn).calculate("AAA", "momentum_12_1", AS_OF)
    assert result.valid is False
    assert result.unavailable_reason == "insufficient_history"


def test_same_day_bar_hidden_until_available_at(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    closes = [Decimal("100")] * REQUIRED_CLOSES
    closes[0] = Decimal("50")
    last = dates[-1]
    morning = datetime(last.year, last.month, last.day, 12, 0, tzinfo=UTC)
    evening = datetime(last.year, last.month, last.day, 23, 0, tzinfo=UTC)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", list(zip(dates, closes, strict=True)))
        svc = MarketAnalyticsService(conn)
        hidden = svc.calculate("AAA", "momentum_12_1", morning)
        visible = svc.calculate("AAA", "momentum_12_1", evening)
    # 16:15 ET is 21:15 UTC in December, so 12:00 UTC cannot see that session.
    assert hidden.valid is False
    assert visible.valid
    assert visible.value == pytest.approx(1.0)


def test_older_known_bars_usable_after_as_of(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10")) for d in dates])
        svc = MarketAnalyticsService(conn)
        early = svc.calculate("AAA", "volatility_1y", datetime(2024, 6, 1, tzinfo=UTC))
        ready = svc.calculate("AAA", "volatility_1y", AS_OF)
    assert early.valid is False
    assert ready.valid


def test_provenance_fields(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10")) for d in dates])
        result = MarketAnalyticsService(conn).calculate("AAA", "max_drawdown_1y", AS_OF)
    assert result.provider == MarketDataProviderName.TWELVE_DATA
    assert result.adjustment_mode == PriceAdjustmentMode.ALL
    assert result.window_start == dates[0]
    assert result.window_end == dates[-1]
    assert result.observation_count == REQUIRED_CLOSES
    assert result.first_trading_date == dates[0]
    assert result.last_trading_date == dates[-1]
    assert result.latest_available_at is not None
    assert ADJUSTED_HISTORY_WARNING in result.warnings


def test_ranking_reversed_ties_unavailable_and_beta_refusal(db: Database) -> None:
    dates = _weekdays_ending_on(AS_OF.date(), REQUIRED_CLOSES)
    with db.session() as conn:
        _seed_symbol(conn, "AAA", [(d, Decimal("10")) for d in dates])
        _seed_symbol(conn, "BBB", [(d, Decimal("10")) for d in dates])
        mmm_closes = [Decimal("10")] * REQUIRED_CLOSES
        mmm_closes[-1] = Decimal("5")
        _seed_symbol(conn, "MMM", list(zip(dates, mmm_closes, strict=True)))
        svc = MarketAnalyticsService(conn)
        forward = svc.rank(["MMM", "BBB", "AAA", "ZZZ"], "max_drawdown_1y", AS_OF)
        reverse = svc.rank(["ZZZ", "AAA", "BBB", "MMM"], "max_drawdown_1y", AS_OF)
        with pytest.raises(BetaHasNoRankingDirection):
            svc.rank(["AAA"], "beta_1y", AS_OF)
    tickers = [row.result.ticker for row in forward.rows]
    assert tickers == [row.result.ticker for row in reverse.rows]
    assert tickers[:2] == ["AAA", "BBB"]
    assert tickers[-1] == "MMM"
    assert forward.rows[0].rank == forward.rows[1].rank == 1
    assert any(symbol == "ZZZ" for symbol, _ in forward.excluded)
    assert all(row.result.value is not None and row.result.value <= 0 for row in forward.rows)
