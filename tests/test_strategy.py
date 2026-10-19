"""ResearchStrategy canonicalization and economic fingerprint tests."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from equitytrace.market.models import PriceAdjustmentMode
from equitytrace.portfolio.models import BacktestRequest, PortfolioBaseline, PortfolioSchedule
from equitytrace.strategy.io import StrategyIOError, load_strategy, strategy_to_canonical_json
from equitytrace.strategy.models import (
    ResearchStrategy,
    fingerprint_request,
    fingerprint_strategy,
)


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _base(**overrides: object) -> ResearchStrategy:
    payload: dict[str, object] = {
        "name": "Demo",
        "tickers": ["AAA", "BBB"],
        "factor": "roa",
        "top_n": 2,
        "baseline": "equal_weight",
        "start_date": "2024-01-02",
        "end_date": "2024-01-31",
        "schedule": "monthly",
    }
    payload.update(overrides)
    return ResearchStrategy.model_validate(payload)


def test_valid_strategy() -> None:
    strategy = _base()
    assert strategy.tickers == ("AAA", "BBB")
    assert strategy.factor == "roa"
    assert strategy.adjustment_mode is PriceAdjustmentMode.ALL


def test_unknown_json_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ResearchStrategy.model_validate(
            {
                "name": "Demo",
                "tickers": ["AAA"],
                "factor": "roa",
                "top_n": 1,
                "baseline": "equal_weight",
                "start_date": "2024-01-02",
                "end_date": "2024-01-31",
                "schedule": "monthly",
                "typo_field": 1,
            }
        )


def test_blank_name_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(name="   ")


def test_empty_universe_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(tickers=[" ", ""])


def test_ticker_normalization_sort_and_dedupe() -> None:
    strategy = _base(tickers=["msft", " AAPL ", "MSFT", "aapl"])
    assert strategy.tickers == ("AAPL", "MSFT")


def test_factor_alias_normalization() -> None:
    strategy = _base(factor="fcf-yield", top_n=1, tickers=["AAA"])
    assert strategy.factor == "fcf_yield"


def test_top_n_must_fit_universe() -> None:
    with pytest.raises(ValidationError):
        _base(top_n=3, tickers=["AAA", "BBB"])


def test_start_after_end_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(start_date="2024-02-01", end_date="2024-01-01")


def test_explicit_schedule_requires_dates() -> None:
    with pytest.raises(ValidationError):
        _base(schedule="explicit", explicit_dates=[])


def test_non_explicit_rejects_dates() -> None:
    with pytest.raises(ValidationError):
        _base(schedule="monthly", explicit_dates=["2024-01-02"])


def test_explicit_dates_sorted_and_deduped() -> None:
    strategy = _base(
        schedule="explicit",
        explicit_dates=["2024-01-15", "2024-01-02", "2024-01-15"],
    )
    assert strategy.explicit_dates == (date(2024, 1, 2), date(2024, 1, 15))


def test_explicit_date_outside_range_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(
            schedule="explicit",
            start_date="2024-01-02",
            end_date="2024-01-10",
            explicit_dates=["2024-01-20"],
        )


def test_calendar_and_benchmark_normalization() -> None:
    strategy = _base(calendar_symbol=" spy ", benchmark_symbol=" qqq ")
    assert strategy.calendar_symbol == "SPY"
    assert strategy.benchmark_symbol == "QQQ"
    cleared = _base(benchmark_symbol="  ")
    assert cleared.benchmark_symbol is None
    null_bench = _base(benchmark_symbol=None)
    assert null_bench.benchmark_symbol is None


def test_blank_calendar_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(calendar_symbol=" ")


def test_cost_and_nav_validation() -> None:
    with pytest.raises(ValidationError):
        _base(cost_bps=-1)
    with pytest.raises(ValidationError):
        _base(initial_nav=0)


def test_raw_adjustment_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(adjustment_mode="none")


def test_to_backtest_request_projection() -> None:
    strategy = _base(tickers=["bbb", "aaa"], factor="fcf_yield", top_n=1)
    request = strategy.to_backtest_request()
    assert request.tickers == ("AAA", "BBB")
    assert request.factor == "fcf_yield"
    assert request.top_n == 1
    assert request.adjustment_mode is PriceAdjustmentMode.ALL
    assert request.baseline is PortfolioBaseline.EQUAL_WEIGHT
    assert request.schedule is PortfolioSchedule.MONTHLY


def test_canonical_json_deterministic() -> None:
    strategy = _base(description="  note  ")
    first = strategy_to_canonical_json(strategy)
    second = strategy_to_canonical_json(ResearchStrategy.model_validate_json(first))
    assert first == second
    assert '"description":"note"' in first


def test_fingerprint_stable_under_non_economic_differences() -> None:
    base = fingerprint_strategy(_base(name="One", description="a"))
    assert base == fingerprint_strategy(_base(name="Two", description="b"))
    assert base == fingerprint_strategy(_base(tickers=["BBB", "AAA", "aaa"]))
    assert base == fingerprint_strategy(_base(tickers=["aaa", "BBB"]))
    explicit_a = fingerprint_strategy(
        _base(
            schedule="explicit",
            explicit_dates=["2024-01-15", "2024-01-02", "2024-01-02"],
        )
    )
    explicit_b = fingerprint_strategy(
        _base(
            schedule="explicit",
            explicit_dates=["2024-01-02", "2024-01-15"],
        )
    )
    assert explicit_a == explicit_b
    alias = fingerprint_strategy(_base(factor="fcf-yield", top_n=1, tickers=["AAA"]))
    assert alias == fingerprint_strategy(_base(factor="fcf_yield", top_n=1, tickers=["AAA"]))
    compact = json.loads(strategy_to_canonical_json(_base(name="One")))
    pretty = json.loads(json.dumps(compact, indent=4, sort_keys=False))
    assert fingerprint_strategy(ResearchStrategy.model_validate(compact)) == fingerprint_strategy(
        ResearchStrategy.model_validate(pretty)
    )


def test_fingerprint_changes_for_economic_fields() -> None:
    base = fingerprint_strategy(_base())
    assert base != fingerprint_strategy(_base(factor="debt_change"))
    assert base != fingerprint_strategy(_base(top_n=1))
    assert base != fingerprint_strategy(_base(baseline="inverse_vol"))
    assert base != fingerprint_strategy(_base(start_date="2024-01-03"))
    assert base != fingerprint_strategy(_base(end_date="2024-01-30"))
    assert base != fingerprint_strategy(_base(schedule="quarterly"))
    assert base != fingerprint_strategy(_base(calendar_symbol="QQQ"))
    assert base != fingerprint_strategy(_base(benchmark_symbol=None))
    assert base != fingerprint_strategy(_base(cost_bps=5.0))
    assert base != fingerprint_strategy(_base(initial_nav=2.0))
    assert base != fingerprint_strategy(_base(tickers=["AAA"], top_n=1))
    payload = _base().to_backtest_request().model_dump(mode="json")
    assert payload["provider"] == "twelve_data"
    assert payload["adjustment_mode"] == "all"
    changed_provider = dict(payload)
    changed_provider["provider"] = "other_provider"
    changed_adjustment = dict(payload)
    changed_adjustment["adjustment_mode"] = "none"
    assert _digest(payload) != _digest(changed_provider)
    assert _digest(payload) == fingerprint_request(_base().to_backtest_request())
    assert _digest(payload) != _digest(changed_adjustment)
    left = fingerprint_strategy(_base(schedule="explicit", explicit_dates=["2024-01-02"]))
    right = fingerprint_strategy(_base(schedule="explicit", explicit_dates=["2024-01-03"]))
    assert left != right


def test_direct_requests_share_canonical_fingerprint() -> None:
    messy = BacktestRequest(
        tickers=("msft", " AAPL ", "MSFT", "aapl"),
        factor="fcf-yield",
        top_n=1,
        baseline=PortfolioBaseline.EQUAL_WEIGHT,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 31),
        schedule=PortfolioSchedule.EXPLICIT,
        explicit_dates=(date(2024, 1, 15), date(2024, 1, 2), date(2024, 1, 2)),
        calendar_symbol="spy",
        benchmark_symbol=" spy ",
    )
    clean = BacktestRequest(
        tickers=("AAPL", "MSFT"),
        factor="fcf_yield",
        top_n=1,
        baseline=PortfolioBaseline.EQUAL_WEIGHT,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 31),
        schedule=PortfolioSchedule.EXPLICIT,
        explicit_dates=(date(2024, 1, 2), date(2024, 1, 15)),
        calendar_symbol="SPY",
        benchmark_symbol="SPY",
    )
    assert messy.tickers != clean.tickers
    assert messy.factor != clean.factor
    assert fingerprint_request(messy) == fingerprint_request(clean)
    assert fingerprint_request(messy) != fingerprint_request(clean.model_copy(update={"top_n": 2}))


@pytest.mark.parametrize(
    "value",
    [
        1704153600,
        1704153600.0,
        "2024-01-02T00:00:00",
        "2024-01-02T00:00:00Z",
        "2024-01-02T00:00:00+00:00",
        "20240102",
        "2024-W01-1",
        "2024-02-31",
        "2023-02-29",
        datetime(2024, 1, 2),
        datetime(2024, 1, 2, 0, 0),
    ],
)
def test_strategy_dates_reject_non_calendar_forms(value: object) -> None:
    with pytest.raises(ValidationError):
        _base(start_date=value)
    with pytest.raises(ValidationError):
        _base(schedule="explicit", explicit_dates=[value])


def test_strategy_dates_accept_calendar_dates() -> None:
    leap = _base(start_date="2024-02-29", end_date="2024-03-01")
    assert leap.start_date == date(2024, 2, 29)
    assert leap.end_date == date(2024, 3, 1)
    programmed = _base(start_date=date(2024, 1, 2), end_date=date(2024, 1, 31))
    assert programmed.start_date == date(2024, 1, 2)
    explicit = _base(
        schedule="explicit",
        explicit_dates=[date(2024, 1, 15), "2024-01-02"],
    )
    assert explicit.explicit_dates == (date(2024, 1, 2), date(2024, 1, 15))


def test_strategy_json_rejects_unsafe_input(tmp_path: Path) -> None:
    valid = {
        "name": "File",
        "tickers": ["bbb", "aaa"],
        "factor": "roa",
        "top_n": 1,
        "baseline": "equal_weight",
        "start_date": "2024-01-02",
        "end_date": "2024-01-31",
        "schedule": "monthly",
    }
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(valid), encoding="utf-8")
    assert load_strategy(ok).tickers == ("AAA", "BBB")

    invalid_utf8 = tmp_path / "bad-encoding.json"
    invalid_utf8.write_bytes(b'{"name": "\xff"}')
    with pytest.raises(StrategyIOError, match="UTF-8"):
        load_strategy(invalid_utf8)

    duplicate_root = tmp_path / "dup-root.json"
    duplicate_root.write_text(
        '{"name": "File", "factor": "roa", "factor": "fcf_yield", '
        '"tickers": ["AAA"], "top_n": 1, "baseline": "equal_weight", '
        '"start_date": "2024-01-02", "end_date": "2024-01-31", "schedule": "monthly"}',
        encoding="utf-8",
    )
    with pytest.raises(StrategyIOError, match="Duplicate JSON key: factor"):
        load_strategy(duplicate_root)

    duplicate_nested = tmp_path / "dup-nested.json"
    body = dict(valid)
    duplicate_nested.write_text(
        json.dumps(body)[:-1] + ', "notes": {"a": 1, "a": 2}}',
        encoding="utf-8",
    )
    with pytest.raises(StrategyIOError, match="Duplicate JSON key: a"):
        load_strategy(duplicate_nested)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(StrategyIOError, match="Invalid JSON"):
        load_strategy(malformed)


def test_fingerprint_equals_request_helper() -> None:
    strategy = _base()
    assert fingerprint_strategy(strategy) == fingerprint_request(strategy.to_backtest_request())


def test_fingerprint_deterministic_under_hash_seed() -> None:
    """CI hash-seed subset: fingerprint must not depend on PYTHONHASHSEED."""
    strategy = _base(tickers=["ZZZ", "AAA", "MMM"], factor="roa", top_n=2)
    digest = fingerprint_strategy(strategy)
    assert len(digest) == 64
    assert digest == fingerprint_request(strategy.to_backtest_request())
    # Recompute via dump/load to ensure JSON key order independence.
    payload = json.loads(strategy_to_canonical_json(strategy))
    shuffled = {key: payload[key] for key in sorted(payload, reverse=True)}
    reloaded = ResearchStrategy.model_validate(shuffled)
    assert fingerprint_strategy(reloaded) == digest


def test_load_strategy_file(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "name": "File",
                "tickers": ["bbb", "aaa"],
                "factor": "roa",
                "top_n": 1,
                "baseline": "equal_weight",
                "start_date": "2024-01-02",
                "end_date": "2024-01-31",
                "schedule": "monthly",
            }
        ),
        encoding="utf-8",
    )
    strategy = load_strategy(path)
    assert strategy.tickers == ("AAA", "BBB")
    assert strategy.top_n == 1


def test_load_strategy_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(StrategyIOError, match="not found"):
        load_strategy(missing)
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(StrategyIOError, match="Invalid JSON"):
        load_strategy(bad)
    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        json.dumps(
            {
                "name": "X",
                "tickers": ["AAA"],
                "factor": "roa",
                "top_n": 1,
                "baseline": "equal_weight",
                "start_date": "2024-01-02",
                "end_date": "2024-01-31",
                "schedule": "monthly",
                "extra": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StrategyIOError, match="Invalid strategy"):
        load_strategy(unknown)
