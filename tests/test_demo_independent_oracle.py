"""Independent oracle for the shipped demo strategies.

Everything the real engine persisted is recomputed here from the *raw synthetic specification*
(fundamentals and close prices) with separate, deliberately simple code: no repositories,
no ``FactorEngine``, no ``BacktestEngine``, no skfolio. Agreement is therefore evidence that the
engine, persistence and the UI's source data match the documented rules (availability cut-off,
newest public annual period, top-N, drifting weights, costs charged only on transitions).
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from equitytrace.demo.data import CompanyBundle, DemoDataset, build_dataset, trading_sessions
from equitytrace.demo.service import DemoService
from equitytrace.portfolio.models import BacktestResult, PortfolioRunStatus, RebalanceStatus

EASTERN = ZoneInfo("America/New_York")
TRADING_DAYS = 252


def _decision_at(day: date) -> datetime:
    return datetime.combine(day, time(16, 15), tzinfo=EASTERN).astimezone(UTC)


@dataclass
class Oracle:
    dataset: DemoDataset
    sessions: list[date]
    closes: dict[str, dict[date, float]]
    company: dict[str, CompanyBundle]

    @classmethod
    def build(cls) -> Oracle:
        dataset = build_dataset()
        closes = {
            s.symbol: {b.trading_date: float(b.close) for b in s.bars} for s in dataset.series
        }
        company = {t.ticker: c for c in dataset.companies for t in c.securities}
        return cls(dataset, list(trading_sessions()), closes, company)

    # ---------------------------------------------------------------- fundamentals
    def newest_public_year(self, ticker: str, decision: datetime) -> int | None:
        years = [
            int(f.report_date.year)
            for f in self.company[ticker].filings
            if f.available_at <= decision and f.report_date is not None
        ]
        return max(years) if years else None

    def factor(self, name: str, ticker: str, year: int) -> float:
        years = self.company[ticker].spec.years
        cur, prior = years[year], years[year - 1]
        if name == "roa":
            return cur.net_income / ((cur.total_assets + prior.total_assets) / 2.0)
        if name == "revenue_growth":
            return cur.revenue / prior.revenue - 1.0
        if name == "operating_margin_change":
            return cur.operating_income / cur.revenue - prior.operating_income / prior.revenue
        raise AssertionError(name)

    def top_n(self, name: str, universe: list[str], decision_day: date, n: int) -> list[str]:
        decision = _decision_at(decision_day)
        scored: list[tuple[float, str]] = []
        for ticker in universe:
            year = self.newest_public_year(ticker, decision)
            assert year is not None and year >= 2022
            scored.append((self.factor(name, ticker, year), ticker))
        scored.sort(key=lambda item: (-item[0], item[1]))  # higher is better, ticker breaks ties
        values = [v for v, _ in scored]
        assert len(set(values)) == len(values), "oracle assumes untied scores"
        return sorted(ticker for _, ticker in scored[:n])

    # --------------------------------------------------------------------- prices
    def returns_matrix(self, names: list[str], decision_day: date) -> np.ndarray:
        eligible = [d for d in self.sessions if d <= decision_day][-(TRADING_DAYS + 1) :]
        assert len(eligible) == TRADING_DAYS + 1
        prices = np.array([[self.closes[n][d] for n in names] for d in eligible])
        return prices[1:] / prices[:-1] - 1.0

    def inverse_vol(self, names: list[str], decision_day: date) -> dict[str, float]:
        matrix = self.returns_matrix(names, decision_day)
        raw = [1.0 / statistics.stdev(matrix[:, i].tolist()) for i in range(len(names))]
        total = sum(raw)
        return {n: r / total for n, r in zip(names, raw, strict=True)}

    def min_variance(self, names: list[str], decision_day: date) -> dict[str, float]:
        """Exact long-only minimum variance by enumerating active sets (<= 3 assets)."""
        cov = np.cov(self.returns_matrix(names, decision_day), rowvar=False)
        best: tuple[float, np.ndarray] | None = None
        for size in range(1, len(names) + 1):
            for subset in itertools.combinations(range(len(names)), size):
                sub = cov[np.ix_(subset, subset)]
                raw = np.linalg.solve(sub, np.ones(size))
                weights = raw / raw.sum()
                if (weights < -1e-12).any():
                    continue
                full = np.zeros(len(names))
                full[list(subset)] = weights
                variance = float(full @ cov @ full)
                if best is None or variance < best[0]:
                    best = (variance, full)
        assert best is not None
        return {n: float(w) for n, w in zip(names, best[1], strict=True)}


@dataclass
class Simulated:
    final_nav: float
    navs: list[float]
    benchmark: list[float]
    rebalances: list[dict[str, object]]
    nav_events: list[float]


def _decisions(oracle: Oracle, schedule: str, start: date, end: date) -> dict[date, date]:
    window = [d for d in oracle.sessions if start <= d <= end]
    chosen: dict[tuple[int, int], date] = {}
    for day in window:
        if schedule == "quarterly" and day.month not in (3, 6, 9, 12):
            continue
        chosen[(day.year, day.month)] = day
    pairs: dict[date, date] = {}
    for decision in chosen.values():
        later = [d for d in oracle.sessions if d > decision]
        if later and later[0] <= end:
            pairs[later[0]] = decision  # effective session -> decision session
    return pairs


def simulate(oracle: Oracle, payload: dict[str, object]) -> Simulated:
    start = date.fromisoformat(str(payload["start_date"]))
    end = date.fromisoformat(str(payload["end_date"]))
    universe = sorted(str(t) for t in payload["tickers"])
    cost_rate = float(payload["cost_bps"]) / 10_000.0
    baseline: Callable[[list[str], date], dict[str, float]]
    if payload["baseline"] == "equal_weight":

        def baseline(names: list[str], _day: date) -> dict[str, float]:
            return {n: 1.0 / len(names) for n in names}

    elif payload["baseline"] == "inverse_vol":
        baseline = oracle.inverse_vol
    else:
        baseline = oracle.min_variance
    effective = _decisions(oracle, str(payload["schedule"]), start, end)
    window = [d for d in oracle.sessions if start <= d <= end]

    nav, cash = float(payload["initial_nav"]), 1.0
    bench = nav
    weights: dict[str, float] = {}
    navs: list[float] = []
    benches: list[float] = []
    events = [nav]
    rebalances: list[dict[str, object]] = []
    previous: date | None = None
    for day in window:
        if previous is not None:
            returns = {k: oracle.closes[k][day] / oracle.closes[k][previous] - 1.0 for k in weights}
            growth = cash + sum(w * (1.0 + returns[k]) for k, w in weights.items())
            nav *= growth
            weights = {k: w * (1.0 + returns[k]) / growth for k, w in weights.items()}
            cash /= growth
            events.append(nav)
            bench *= oracle.closes["SYNX"][day] / oracle.closes["SYNX"][previous]
        if day in effective:
            decision_day = effective[day]
            names = oracle.top_n(
                str(payload["factor"]), universe, decision_day, int(payload["top_n"])
            )
            target = baseline(names, decision_day)
            drifted = dict(weights)
            turnover = sum(
                abs(target.get(k, 0.0) - drifted.get(k, 0.0)) for k in {*target, *drifted}
            )
            cost = nav * turnover * cost_rate
            pre = nav
            nav -= cost
            weights, cash = {k: w for k, w in target.items() if w > 0}, 0.0
            events.append(nav)
            rebalances.append(
                {
                    "decision_day": decision_day,
                    "effective_day": day,
                    "names": names,
                    "target": target,
                    "drifted": drifted,
                    "turnover": turnover,
                    "cost": cost,
                    "pre": pre,
                    "post": nav,
                }
            )
        navs.append(nav)
        benches.append(bench)
        previous = day
    return Simulated(nav, navs, benches, rebalances, events)


def max_drawdown(events: list[float]) -> float:
    peak, worst = events[0], 0.0
    for value in events:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


@pytest.fixture(scope="module")
def oracle() -> Oracle:
    return Oracle.build()


@pytest.mark.parametrize(
    ("key", "weight_tolerance"),
    [
        ("roa-equal-monthly", 1e-12),
        ("growth-inverse-vol-quarterly", 1e-9),
        ("margin-min-variance-monthly", 1e-4),
    ],
)
def test_engine_output_matches_an_independent_recomputation(
    demo_dir: Path, oracle: Oracle, key: str, weight_tolerance: float
) -> None:
    service = DemoService(demo_dir)
    payload = next(p.payload for p in service.presets() if p.key == key)
    outcome = service.validate_strategy(payload)
    assert outcome.strategy is not None
    result: BacktestResult = service.run_strategy(outcome.strategy, token=key)
    assert result.status is PortfolioRunStatus.SUCCESS
    expected = simulate(oracle, payload)
    # Solver tolerance in the optimizer (~1e-5 per rebalance) legitimately compounds into the
    # later drifted weights and NAV path; the closed-form baselines must match tightly.
    optimized = key == "margin-min-variance-monthly"
    drift_tolerance = 1e-3 if optimized else 1e-9
    nav_rel = 5e-4 if optimized else 1e-9

    # Every rebalance: decision/effective sessions, names, target and drifted weights, cost.
    successful = [r for r in result.rebalances if r.status is RebalanceStatus.SUCCESS]
    assert len(successful) == len(expected.rebalances)
    for actual, want in zip(successful, expected.rebalances, strict=True):
        assert actual.decision_at == _decision_at(want["decision_day"])
        assert actual.target_effective_at == _decision_at(want["effective_day"])
        held = {t.symbol: t for t in actual.transitions if t.target_weight_after > 0}
        assert sorted(held) == want["names"]
        for symbol, item in held.items():
            assert item.target_weight_after == pytest.approx(
                want["target"][symbol], abs=1e-3 if optimized else weight_tolerance
            )
        if optimized:
            # Near-flat objectives make individual weights imprecise; optimality is the claim.
            names = sorted(held)
            cov = np.cov(oracle.returns_matrix(names, want["decision_day"]), rowvar=False)
            engine_w = np.array([held[n].target_weight_after for n in names])
            oracle_w = np.array([want["target"][n] for n in names])
            assert engine_w.sum() == pytest.approx(1.0, abs=1e-9)
            assert engine_w @ cov @ engine_w <= oracle_w @ cov @ oracle_w * (1 + 1e-5)
        for item in actual.transitions:
            assert item.current_weight_before == pytest.approx(
                want["drifted"].get(item.symbol, 0.0), abs=drift_tolerance
            )
        assert actual.pre_cost_nav == pytest.approx(want["pre"], rel=nav_rel)
        if not optimized:
            assert actual.gross_turnover == pytest.approx(want["turnover"], abs=1e-9)
            assert actual.cost_amount == pytest.approx(want["cost"], abs=1e-12)
            assert actual.post_cost_nav == pytest.approx(want["post"], rel=1e-9)

    # The whole path, the benchmark, and the headline metrics.
    rel = nav_rel
    assert result.final_nav == pytest.approx(expected.final_nav, rel=rel)
    assert [p.nav for p in result.equity] == pytest.approx(expected.navs, rel=rel)
    assert [p.benchmark_nav for p in result.equity] == pytest.approx(expected.benchmark, rel=1e-12)
    assert result.metrics is not None
    metrics = result.metrics
    assert metrics.total_return == pytest.approx(expected.final_nav - 1.0, rel=rel, abs=1e-9)
    assert metrics.max_drawdown == pytest.approx(max_drawdown(expected.nav_events), abs=5 * rel)
    daily = [b / a - 1.0 for a, b in itertools.pairwise(expected.navs)]
    assert metrics.annualized_volatility == pytest.approx(
        statistics.stdev(daily) * math.sqrt(TRADING_DAYS), rel=max(rel, 1e-6)
    )
    assert metrics.annualized_return == pytest.approx(
        expected.final_nav ** (TRADING_DAYS / len(daily)) - 1.0, rel=max(rel, 1e-6)
    )
    assert metrics.sharpe_ratio == pytest.approx(
        statistics.mean(daily) / statistics.stdev(daily) * math.sqrt(TRADING_DAYS),
        rel=max(rel, 1e-5),
    )
    if not optimized:
        assert metrics.gross_turnover == pytest.approx(
            sum(float(r["turnover"]) for r in expected.rebalances), abs=1e-9
        )
        assert metrics.transaction_cost_total == pytest.approx(
            sum(float(r["cost"]) for r in expected.rebalances), abs=1e-12
        )

    # Drawdown column and cash weight (fully invested after the first rebalance).
    peak = 0.0
    for point in result.equity:
        peak = max(peak, point.nav)
        assert point.drawdown == pytest.approx(point.nav / peak - 1.0 if peak else 0.0, abs=1e-9)
    first_effective = expected.rebalances[0]["effective_day"]
    invested = [p.cash_weight for p in result.equity if p.valuation_at.date() >= first_effective]
    assert all(abs(c) < 1e-12 for c in invested)
    assert all(p.cash_weight == 1.0 for p in result.equity[:5])  # in cash before first decision


def test_minimum_variance_oracle_is_actually_minimal(oracle: Oracle) -> None:
    """Sanity-check the oracle itself so agreement with skfolio means something."""
    names = ["SYAL", "SYDL", "SYFM"]
    day = date(2024, 6, 28)
    weights = oracle.min_variance(names, day)
    cov = np.cov(oracle.returns_matrix(names, day), rowvar=False)
    best = np.array([weights[n] for n in names])
    assert best.sum() == pytest.approx(1.0) and (best >= -1e-12).all()
    rng = np.random.default_rng(0)
    for _ in range(2000):
        trial = rng.dirichlet(np.ones(3))
        assert trial @ cov @ trial >= best @ cov @ best - 1e-15
