"""Deterministic weight-return research backtester."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import uuid4

import duckdb

from equitytrace import __version__
from equitytrace.factors.engine import FactorEngine
from equitytrace.factors.models import FactorResult
from equitytrace.factors.registry import UnknownFactorError, get_factor, normalize_factor_name
from equitytrace.market.models import DailyPriceBar, MarketInstrument
from equitytrace.models import utc_now
from equitytrace.portfolio.audit import audit_decision, build_audit_result
from equitytrace.portfolio.baselines import (
    BaselineUnavailable,
    equal_weight,
    inverse_volatility,
)
from equitytrace.portfolio.calendar import (
    CalendarError,
    CalendarSession,
    resolve_rebalance_pairs,
    sessions_from_bars,
)
from equitytrace.portfolio.metrics import compute_metrics
from equitytrace.portfolio.models import (
    SAME_ISSUER_UNAVAILABLE,
    STANDING_WARNINGS,
    WEIGHT_TOLERANCE,
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    FormedTarget,
    LeakageAuditResult,
    PerformanceMetrics,
    PortfolioBaseline,
    PortfolioRunStatus,
    PortfolioState,
    RebalanceRecord,
    RebalanceStatus,
    WeightTransition,
)
from equitytrace.portfolio.returns import aligned_return_matrix, session_return
from equitytrace.portfolio.signals import (
    normalize_universe,
    resolve_latest_annual_factor,
    select_exact_top_n,
    selected_symbols,
    tickers_sharing_an_issuer,
)
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.portfolio import PortfolioRepository


class PortfolioUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PortfolioFailed(RuntimeError):
    def __init__(self, reason: str, audit: LeakageAuditResult | None = None) -> None:
        self.reason = reason
        self.audit = audit
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _TransitionOutcome:
    pre_cost_nav: float
    post_cost_nav: float
    gross_turnover: float
    cost_amount: float


def assert_weights(state: PortfolioState) -> None:
    total = state.cash_weight + sum(
        state.asset_weights[name] for name in sorted(state.asset_weights)
    )
    if not math.isfinite(total) or abs(total - 1.0) > WEIGHT_TOLERANCE:
        raise PortfolioFailed("non_finite_weights")
    if not math.isfinite(state.nav) or state.nav <= 0:
        raise PortfolioFailed("non_finite_nav")


def gross_turnover(current: dict[str, float], target: dict[str, float]) -> float:
    names = sorted(set(current) | set(target))
    return sum(abs(target.get(name, 0.0) - current.get(name, 0.0)) for name in names)


def apply_interval_returns(state: PortfolioState, returns: dict[str, float]) -> None:
    """Drift holdings by interval returns. Cash earns 0. Missing held returns fail."""
    symbols = sorted(name for name, weight in state.asset_weights.items() if weight > 0)
    growth = state.cash_weight
    for symbol in symbols:
        weight = state.asset_weights[symbol]
        if symbol not in returns:
            raise PortfolioFailed("held_return_missing")
        ret = returns[symbol]
        if not math.isfinite(ret):
            raise PortfolioFailed("held_return_missing")
        growth += weight * (1.0 + ret)
    if not math.isfinite(growth) or growth <= 0:
        raise PortfolioFailed("non_positive_growth")
    next_assets: dict[str, float] = {}
    for symbol in symbols:
        ret = returns[symbol]
        next_weight = state.asset_weights[symbol] * (1.0 + ret) / growth
        if next_weight > 0:
            next_assets[symbol] = next_weight
    state.nav = state.nav * growth
    state.cash_weight = state.cash_weight / growth
    state.asset_weights = next_assets
    assert_weights(state)


def apply_weight_transition(
    state: PortfolioState,
    target_weights: dict[str, float],
    *,
    target_cash: float,
    cost_rate: float,
) -> _TransitionOutcome:
    turnover = gross_turnover(state.asset_weights, target_weights)
    pre = state.nav
    cost = pre * turnover * cost_rate
    post = pre - cost
    if not math.isfinite(cost) or cost < 0:
        raise PortfolioFailed("invalid_cost")
    if turnover == 0.0 and cost != 0.0:
        raise PortfolioFailed("invalid_cost")
    if not math.isfinite(post) or post <= 0 or post > pre + 1e-12:
        raise PortfolioFailed("invalid_post_cost_nav")
    state.nav = post
    state.asset_weights = {
        name: target_weights[name] for name in sorted(target_weights) if target_weights[name] > 0
    }
    state.cash_weight = target_cash
    assert_weights(state)
    return _TransitionOutcome(
        pre_cost_nav=pre,
        post_cost_nav=post,
        gross_turnover=turnover,
        cost_amount=cost,
    )


class BacktestEngine:
    """Orchestrate calendar, signals, drift, costs, audit, and persistence."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._factors = FactorEngine(conn)
        self._market = MarketRepository(conn)
        self._repo = PortfolioRepository(conn)

    def run(self, request: BacktestRequest, *, persist: bool = True) -> BacktestResult:
        created = utc_now()
        run_id = uuid4().hex
        try:
            normalize_factor_name(request.factor)
            get_factor(request.factor)
        except UnknownFactorError as exc:
            result = self._terminal(
                run_id,
                request,
                created,
                PortfolioRunStatus.FAILED,
                str(exc),
            )
            return self._persist(result, persist)
        try:
            result = self._execute(run_id, request, created)
        except PortfolioUnavailable as exc:
            result = self._terminal(
                run_id,
                request,
                created,
                PortfolioRunStatus.UNAVAILABLE,
                exc.reason,
            )
        except PortfolioFailed as exc:
            result = self._terminal(
                run_id,
                request,
                created,
                PortfolioRunStatus.FAILED,
                exc.reason,
                audit=exc.audit,
            )
        except CalendarError as exc:
            result = self._terminal(
                run_id,
                request,
                created,
                PortfolioRunStatus.UNAVAILABLE,
                exc.reason,
            )
        except duckdb.Error:
            result = self._terminal(
                run_id,
                request,
                created,
                PortfolioRunStatus.FAILED,
                "database_error",
            )
        if persist:
            try:
                self._repo.persist_run(result)
            except Exception:
                return result.model_copy(
                    update={
                        "status": PortfolioRunStatus.FAILED,
                        "failure_reason": "database_error",
                        "completed_at": utc_now(),
                    }
                )
        return result

    def _execute(
        self,
        run_id: str,
        request: BacktestRequest,
        created: datetime,
    ) -> BacktestResult:
        universe = normalize_universe(request.tickers)
        if not universe:
            raise PortfolioUnavailable("factor_universe_too_small")
        issuer_ciks = self._market.issuer_ciks_for_symbols(universe)
        if tickers_sharing_an_issuer(issuer_ciks):
            raise PortfolioUnavailable(SAME_ISSUER_UNAVAILABLE)

        calendar_symbol = request.calendar_symbol
        needed = list(dict.fromkeys([*universe, calendar_symbol]))
        if request.benchmark_symbol:
            needed.append(request.benchmark_symbol)
        instruments = self._market.get_instruments_by_symbols(needed)
        calendar_instrument = instruments.get(calendar_symbol)
        if calendar_instrument is None:
            raise PortfolioUnavailable("calendar_missing")

        lookback_start = request.start_date - timedelta(days=request_lookback_days())
        instrument_ids = [item.instrument_id for item in instruments.values()]
        raw_bars = self._market.get_price_bars_for_instruments(
            instrument_ids,
            start_date=lookback_start,
            end_date=request.end_date,
            adjustment_mode=request.adjustment_mode,
            provider=request.provider,
        )
        bars_by_symbol = _group_bars(raw_bars, instruments)
        calendar_bars = list(bars_by_symbol.get(calendar_symbol, []))
        _, stored_max = self._market.get_stored_date_range(
            calendar_instrument.instrument_id,
            adjustment_mode=request.adjustment_mode,
            provider=request.provider,
        )
        if stored_max is not None and stored_max > request.end_date:
            extra_calendar = self._market.get_price_bars_for_instruments(
                [calendar_instrument.instrument_id],
                start_date=request.end_date + timedelta(days=1),
                end_date=stored_max,
                adjustment_mode=request.adjustment_mode,
                provider=request.provider,
            )
            calendar_bars.extend(extra_calendar)
        calendar_sessions = sessions_from_bars(calendar_bars)
        window = [
            session
            for session in calendar_sessions
            if request.start_date <= session.trading_date <= request.end_date
        ]
        if not window:
            raise PortfolioUnavailable("calendar_missing")

        pairs = resolve_rebalance_pairs(
            calendar_sessions,
            schedule=request.schedule,
            start_date=request.start_date,
            end_date=request.end_date,
            explicit_dates=request.explicit_dates,
        )
        effective_map = {effective.trading_date: decision for decision, effective in pairs}

        state = PortfolioState(nav=request.initial_nav, cash_weight=1.0, asset_weights={})
        warnings = list(STANDING_WARNINGS)
        mixed = False
        leakage_failures: list[str] = []
        rebalances: list[RebalanceRecord] = []
        equity: list[EquityPoint] = []
        nav_events: list[float] = [request.initial_nav]
        session_navs: list[float] = []
        peak = request.initial_nav
        first_success = False
        benchmark_nav: float | None = request.initial_nav if request.benchmark_symbol else None
        benchmark_ok = request.benchmark_symbol is not None
        prev: CalendarSession | None = None

        for session in window:
            if prev is not None:
                held_returns = self._held_returns(
                    state,
                    bars_by_symbol,
                    prev=prev,
                    current=session,
                )
                apply_interval_returns(state, held_returns)
                state.last_valuation_session = session.trading_date
                peak = max(peak, state.nav)
                nav_events.append(state.nav)
                if benchmark_ok and request.benchmark_symbol:
                    bench_ret = self._symbol_return(
                        bars_by_symbol.get(request.benchmark_symbol, []),
                        prev,
                        session,
                    )
                    if bench_ret is None or benchmark_nav is None:
                        benchmark_ok = False
                        benchmark_nav = None
                        _append_unique(warnings, "benchmark_unavailable")
                    else:
                        benchmark_nav = benchmark_nav * (1.0 + bench_ret)

            decision_session = effective_map.get(session.trading_date)
            if decision_session is not None:
                attempt = self._maybe_rebalance(
                    request=request,
                    state=state,
                    universe=universe,
                    bars_by_symbol=bars_by_symbol,
                    instruments=instruments,
                    decision=decision_session,
                    effective=session,
                    first_success=first_success,
                    leakage_failures=leakage_failures,
                )
                if attempt.record.status is RebalanceStatus.SUCCESS:
                    first_success = True
                    peak = max(peak, attempt.record.pre_cost_nav)
                    nav_events.append(attempt.record.post_cost_nav)
                rebalances.append(attempt.record)
                mixed = mixed or attempt.selection_mixed

            drawdown = state.nav / peak - 1.0
            equity.append(
                EquityPoint(
                    valuation_at=session.available_at,
                    nav=state.nav,
                    cash_weight=state.cash_weight,
                    drawdown=drawdown,
                    benchmark_nav=benchmark_nav if benchmark_ok else None,
                )
            )
            session_navs.append(state.nav)
            prev = session

        if not first_success:
            raise PortfolioUnavailable("factor_universe_too_small")

        if mixed:
            _append_unique(warnings, MIXED)
        extra = tuple(w for w in warnings if w not in STANDING_WARNINGS)
        audit = build_audit_result(
            failures=leakage_failures,
            mixed_periods=mixed,
            extra_warnings=extra,
        )
        if not audit.passed:
            raise PortfolioFailed("leakage_detected", audit=audit)

        metrics = compute_metrics(
            initial_nav=request.initial_nav,
            final_nav=state.nav,
            session_navs=session_navs,
            nav_events=nav_events,
            rebalances=rebalances,
        )
        return BacktestResult(
            run_id=run_id,
            status=PortfolioRunStatus.SUCCESS,
            request=request,
            warnings=tuple(dict.fromkeys(warnings)),
            created_at=created,
            completed_at=utc_now(),
            universe=tuple(universe),
            final_nav=state.nav,
            metrics=metrics,
            rebalances=tuple(rebalances),
            equity=tuple(equity),
            audit=audit,
            package_version=__version__,
        )

    def _maybe_rebalance(
        self,
        *,
        request: BacktestRequest,
        state: PortfolioState,
        universe: list[str],
        bars_by_symbol: dict[str, list[DailyPriceBar]],
        instruments: dict[str, MarketInstrument],
        decision: CalendarSession,
        effective: CalendarSession,
        first_success: bool,
        leakage_failures: list[str],
    ) -> _RebalanceAttempt:
        try:
            target = self._form_target(
                request,
                universe,
                bars_by_symbol,
                decision_at=decision.available_at,
                session_date=decision.trading_date,
            )
        except PortfolioUnavailable as exc:
            if not first_success:
                raise
            return _unavailable_rebalance(decision, effective, state, exc.reason)

        mixed = target.selection.mixed_fiscal_periods
        leakage_failures.extend(
            audit_decision(
                decision_at=decision.available_at,
                decision_session_date=decision.trading_date,
                target_effective_at=effective.available_at,
                selection=target.selection,
                mixed_periods=mixed,
            )
        )
        if not self._targets_supported(target.weights, bars_by_symbol, effective):
            if not first_success:
                raise PortfolioUnavailable("target_support_missing")
            return _unavailable_rebalance(
                decision,
                effective,
                state,
                "target_support_missing",
                mixed=mixed,
            )

        before = dict(state.asset_weights)
        outcome = apply_weight_transition(
            state,
            target.weights,
            target_cash=target.cash_weight,
            cost_rate=request.cost_rate,
        )
        transitions = _transition_rows(
            before=before,
            target=target,
            instruments=instruments,
            pre_cost_nav=outcome.pre_cost_nav,
            cost=outcome.cost_amount,
            turnover=outcome.gross_turnover,
            decision_at=decision.available_at,
            target_effective_at=effective.available_at,
        )
        record = RebalanceRecord(
            decision_at=decision.available_at,
            target_effective_at=effective.available_at,
            status=RebalanceStatus.SUCCESS,
            pre_cost_nav=outcome.pre_cost_nav,
            post_cost_nav=outcome.post_cost_nav,
            gross_turnover=outcome.gross_turnover,
            cost_amount=outcome.cost_amount,
            transitions=transitions,
        )
        return _RebalanceAttempt(record=record, selection_mixed=mixed)

    def _form_target(
        self,
        request: BacktestRequest,
        universe: list[str],
        bars_by_symbol: dict[str, list[DailyPriceBar]],
        *,
        decision_at: datetime,
        session_date: date,
    ) -> FormedTarget:
        valid: list[FactorResult] = []
        evidence: dict[str, datetime] = {}
        for ticker in universe:
            result, _reason, known = resolve_latest_annual_factor(
                self._factors,
                ticker,
                request.factor,
                decision_at,
            )
            if known is not None:
                evidence[ticker] = known
            if result is None:
                continue
            valid.append(result)
        selection = select_exact_top_n(
            valid,
            factor=request.factor,
            as_of=decision_at,
            top_n=request.top_n,
            evidence_available_at=evidence,
        )
        if selection is None:
            raise PortfolioUnavailable("factor_universe_too_small")
        names = selected_symbols(selection.ranked)
        if request.baseline is PortfolioBaseline.EQUAL_WEIGHT:
            try:
                weights = equal_weight(names)
            except BaselineUnavailable as exc:
                raise PortfolioUnavailable(exc.reason) from exc
            price_at: tuple[datetime, ...] = ()
            price_dates: tuple[date, ...] = ()
        else:
            subset = {name: bars_by_symbol.get(name, []) for name in names}
            matrix = aligned_return_matrix(
                subset,
                as_of=decision_at,
                session_date=session_date,
            )
            if matrix is None:
                raise PortfolioUnavailable("baseline_unavailable")
            try:
                weights = inverse_volatility(matrix)
            except BaselineUnavailable as exc:
                raise PortfolioUnavailable(exc.reason) from exc
            price_at, price_dates = _matrix_evidence(subset, decision_at, session_date)
        selection = selection.model_copy(
            update={
                "price_evidence_available_at": price_at,
                "price_evidence_dates": price_dates,
            }
        )
        return FormedTarget(
            weights=weights,
            cash_weight=0.0,
            selection=selection,
            factor_results=tuple(valid),
        )

    def _targets_supported(
        self,
        weights: dict[str, float],
        bars_by_symbol: dict[str, list[DailyPriceBar]],
        session: CalendarSession,
    ) -> bool:
        for symbol in sorted(weights):
            if weights[symbol] <= 0:
                continue
            bar = _bar_on_session(bars_by_symbol.get(symbol, []), session)
            if bar is None:
                return False
        return True

    def _held_returns(
        self,
        state: PortfolioState,
        bars_by_symbol: dict[str, list[DailyPriceBar]],
        *,
        prev: CalendarSession,
        current: CalendarSession,
    ) -> dict[str, float]:
        returns: dict[str, float] = {}
        for symbol in sorted(state.asset_weights):
            if state.asset_weights[symbol] <= 0:
                continue
            value = self._symbol_return(bars_by_symbol.get(symbol, []), prev, current)
            if value is None:
                raise PortfolioFailed("held_return_missing")
            returns[symbol] = value
        return returns

    def _symbol_return(
        self,
        bars: list[DailyPriceBar],
        prev: CalendarSession,
        current: CalendarSession,
    ) -> float | None:
        prev_bar = _bar_on_session(bars, prev)
        curr_bar = _bar_on_session(bars, current)
        if prev_bar is None or curr_bar is None:
            return None
        return session_return(float(prev_bar.close), float(curr_bar.close))

    def _terminal(
        self,
        run_id: str,
        request: BacktestRequest,
        created: datetime,
        status: PortfolioRunStatus,
        reason: str,
        audit: LeakageAuditResult | None = None,
    ) -> BacktestResult:
        if audit is None:
            audit = build_audit_result(failures=[], mixed_periods=False)
        return BacktestResult(
            run_id=run_id,
            status=status,
            request=request,
            failure_reason=reason,
            warnings=tuple(audit.warnings),
            created_at=created,
            completed_at=utc_now(),
            universe=tuple(normalize_universe(request.tickers)),
            metrics=PerformanceMetrics(),
            audit=audit,
            package_version=__version__,
        )

    def _persist(self, result: BacktestResult, persist: bool) -> BacktestResult:
        if not persist:
            return result
        try:
            self._repo.persist_run(result)
        except Exception:
            return result.model_copy(
                update={
                    "status": PortfolioRunStatus.FAILED,
                    "failure_reason": "database_error",
                    "completed_at": utc_now(),
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class _RebalanceAttempt:
    record: RebalanceRecord
    selection_mixed: bool = False


def request_lookback_days() -> int:
    from equitytrace.portfolio.models import LOOKBACK_CALENDAR_DAYS

    return LOOKBACK_CALENDAR_DAYS


MIXED = "mixed_fiscal_periods"


def _append_unique(values: list[str], item: str) -> None:
    if item and item not in values:
        values.append(item)


def _group_bars(
    bars: list[DailyPriceBar],
    instruments: dict[str, MarketInstrument],
) -> dict[str, list[DailyPriceBar]]:
    by_id = {item.instrument_id: item.canonical_symbol for item in instruments.values()}
    grouped: dict[str, list[DailyPriceBar]] = defaultdict(list)
    for bar in bars:
        symbol = by_id.get(bar.instrument_id)
        if symbol is None:
            continue
        grouped[symbol].append(bar)
    return grouped


def _bar_on_session(
    bars: list[DailyPriceBar],
    session: CalendarSession,
) -> DailyPriceBar | None:
    matches = [
        bar
        for bar in bars
        if bar.trading_date == session.trading_date and bar.available_at <= session.available_at
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _matrix_evidence(
    bars_by_symbol: dict[str, list[DailyPriceBar]],
    decision_at: datetime,
    session_date: date,
) -> tuple[tuple[datetime, ...], tuple[date, ...]]:
    known: list[datetime] = []
    days = []
    for bars in bars_by_symbol.values():
        for bar in bars:
            if bar.available_at <= decision_at and bar.trading_date <= session_date:
                known.append(bar.available_at)
                days.append(bar.trading_date)
    return tuple(known), tuple(days)


def _unavailable_rebalance(
    decision: CalendarSession,
    effective: CalendarSession,
    state: PortfolioState,
    reason: str,
    *,
    mixed: bool = False,
) -> _RebalanceAttempt:
    record = RebalanceRecord(
        decision_at=decision.available_at,
        target_effective_at=effective.available_at,
        status=RebalanceStatus.UNAVAILABLE,
        reason=reason,
        pre_cost_nav=state.nav,
        post_cost_nav=state.nav,
        gross_turnover=0.0,
        cost_amount=0.0,
        transitions=(),
    )
    return _RebalanceAttempt(record=record, selection_mixed=mixed)


def _signal_provenance_json(result: FactorResult) -> str:
    payload = {
        "factor": result.factor,
        "ticker": result.ticker,
        "cik": result.cik,
        "as_of": result.as_of.isoformat(),
        "period": result.period.label(),
        "ranking_direction": result.ranking_direction,
        "inputs": result.inputs,
        "source_filings": list(result.source_filings),
        "warnings": list(result.warnings),
        "unavailable_reason": result.unavailable_reason,
        "market_input": (
            result.market_input.model_dump(mode="json") if result.market_input is not None else None
        ),
    }
    return json.dumps(payload, sort_keys=True)


def _transition_rows(
    *,
    before: dict[str, float],
    target: FormedTarget,
    instruments: dict[str, MarketInstrument],
    pre_cost_nav: float,
    cost: float,
    turnover: float,
    decision_at: datetime,
    target_effective_at: datetime,
) -> tuple[WeightTransition, ...]:
    ranked = {row.result.ticker: row for row in target.selection.ranked}
    names = sorted(set(before) | set(target.weights))
    rows: list[WeightTransition] = []
    for symbol in names:
        current = before.get(symbol, 0.0)
        after = target.weights.get(symbol, 0.0)
        delta = after - current
        notional = abs(delta) * pre_cost_nav
        allocated = 0.0 if turnover == 0 else cost * (abs(delta) / turnover)
        instrument = instruments.get(symbol)
        instrument_id = instrument.instrument_id if instrument is not None else ""
        ranked_row = ranked.get(symbol)
        provenance = None
        signal_value = None
        signal_rank = None
        signal_period = None
        if ranked_row is not None:
            signal_value = ranked_row.result.value
            signal_rank = ranked_row.rank
            signal_period = ranked_row.result.period.label()
            provenance = _signal_provenance_json(ranked_row.result)
        rows.append(
            WeightTransition(
                symbol=symbol,
                instrument_id=instrument_id,
                current_weight_before=current,
                target_weight_after=after,
                delta_weight=delta,
                gross_notional=notional,
                allocated_cost=allocated,
                signal_value=signal_value,
                signal_rank=signal_rank,
                signal_period=signal_period,
                signal_provenance_json=provenance,
            )
        )
    return tuple(rows)
