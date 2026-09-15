"""Persistence for native portfolio backtest runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb

from equitytrace.portfolio.models import (
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    PerformanceMetrics,
    PortfolioRunStatus,
    RebalanceRecord,
    RebalanceStatus,
    WeightTransition,
)


class PortfolioRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def persist_run(self, result: BacktestResult) -> None:
        """Write a completed run and children atomically."""
        self._conn.execute("BEGIN TRANSACTION")
        try:
            self._upsert_run(result)
            for rebalance in result.rebalances:
                self._insert_rebalance(result.run_id, rebalance)
                for row in rebalance.transitions:
                    self._insert_transition(
                        result.run_id,
                        rebalance.decision_at,
                        rebalance.target_effective_at,
                        row,
                    )
            for point in result.equity:
                self._insert_equity(result.run_id, point)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_run(self, run_id: str) -> BacktestResult | None:
        row = self._conn.execute(
            """
            SELECT run_id, created_at, completed_at, status, failure_reason,
                   start_date, end_date, schedule, calendar_symbol, benchmark_symbol,
                   factor_name, top_n, baseline, cost_bps, initial_nav, provider,
                   adjustment_mode, warnings_json, audit_json, package_version,
                   request_json, final_nav, metrics_json
            FROM portfolio_runs
            WHERE run_id = ?
            """,
            [run_id],
        ).fetchone()
        if row is None:
            return None
        request = BacktestRequest.model_validate_json(str(row[20]))
        metrics = (
            PerformanceMetrics.model_validate_json(str(row[22])) if row[22] is not None else None
        )
        rebalances = self._load_rebalances(str(row[0]))
        equity = self._load_equity(str(row[0]))
        audit = None
        if row[18]:
            from equitytrace.portfolio.models import LeakageAuditResult

            audit = LeakageAuditResult.model_validate_json(str(row[18]))
        warnings = tuple(json.loads(str(row[17]))) if row[17] else ()
        return BacktestResult(
            run_id=str(row[0]),
            status=PortfolioRunStatus(str(row[3])),
            request=request,
            failure_reason=str(row[4]) if row[4] is not None else None,
            warnings=warnings,
            created_at=_as_utc(row[1]),
            completed_at=_as_utc(row[2]) if row[2] is not None else None,
            universe=tuple(normalize_from_request(request)),
            final_nav=float(row[21]) if row[21] is not None else None,
            metrics=metrics,
            rebalances=rebalances,
            equity=equity,
            audit=audit,
            package_version=str(row[19]),
        )

    def _upsert_run(self, result: BacktestResult) -> None:
        request = result.request
        self._conn.execute(
            """
            INSERT INTO portfolio_runs (
                run_id, created_at, completed_at, status, failure_reason,
                start_date, end_date, schedule, calendar_symbol, benchmark_symbol,
                factor_name, top_n, baseline, cost_bps, initial_nav, provider,
                adjustment_mode, warnings_json, audit_json, package_version,
                request_json, final_nav, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                result.run_id,
                result.created_at,
                result.completed_at,
                result.status.value,
                result.failure_reason,
                request.start_date,
                request.end_date,
                request.schedule.value,
                request.calendar_symbol,
                request.benchmark_symbol,
                request.factor,
                request.top_n,
                request.baseline.value,
                request.cost_bps,
                request.initial_nav,
                request.provider.value,
                request.adjustment_mode.value,
                json.dumps(list(result.warnings)),
                result.audit.model_dump_json() if result.audit is not None else None,
                result.package_version,
                request.model_dump_json(),
                result.final_nav,
                result.metrics.model_dump_json() if result.metrics is not None else None,
            ],
        )

    def _insert_rebalance(self, run_id: str, rebalance: RebalanceRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO portfolio_rebalances (
                run_id, decision_at, target_effective_at, status, reason,
                pre_cost_nav, post_cost_nav, gross_turnover, cost_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                rebalance.decision_at,
                rebalance.target_effective_at,
                rebalance.status.value,
                rebalance.reason,
                rebalance.pre_cost_nav,
                rebalance.post_cost_nav,
                rebalance.gross_turnover,
                rebalance.cost_amount,
            ],
        )

    def _insert_transition(
        self,
        run_id: str,
        decision_at: datetime,
        target_effective_at: datetime,
        row: WeightTransition,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO portfolio_weight_transitions (
                run_id, decision_at, target_effective_at, symbol, instrument_id,
                current_weight_before, target_weight_after, delta_weight,
                gross_notional, allocated_cost, signal_value, signal_rank,
                signal_period, signal_provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                decision_at,
                target_effective_at,
                row.symbol,
                row.instrument_id,
                row.current_weight_before,
                row.target_weight_after,
                row.delta_weight,
                row.gross_notional,
                row.allocated_cost,
                row.signal_value,
                row.signal_rank,
                row.signal_period,
                row.signal_provenance_json,
            ],
        )

    def _insert_equity(self, run_id: str, point: EquityPoint) -> None:
        self._conn.execute(
            """
            INSERT INTO portfolio_equity (
                run_id, valuation_at, nav, cash_weight, drawdown, benchmark_nav
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                point.valuation_at,
                point.nav,
                point.cash_weight,
                point.drawdown,
                point.benchmark_nav,
            ],
        )

    def _load_rebalances(self, run_id: str) -> tuple[RebalanceRecord, ...]:
        rows = self._conn.execute(
            """
            SELECT decision_at, target_effective_at, status, reason,
                   pre_cost_nav, post_cost_nav, gross_turnover, cost_amount
            FROM portfolio_rebalances
            WHERE run_id = ?
            ORDER BY decision_at
            """,
            [run_id],
        ).fetchall()
        out: list[RebalanceRecord] = []
        for row in rows:
            decision_at = _as_utc(row[0])
            transitions = self._load_transitions(run_id, decision_at)
            out.append(
                RebalanceRecord(
                    decision_at=decision_at,
                    target_effective_at=_as_utc(row[1]),
                    status=RebalanceStatus(str(row[2])),
                    reason=str(row[3]) if row[3] is not None else None,
                    pre_cost_nav=float(row[4]),
                    post_cost_nav=float(row[5]),
                    gross_turnover=float(row[6]),
                    cost_amount=float(row[7]),
                    transitions=transitions,
                )
            )
        return tuple(out)

    def _load_transitions(self, run_id: str, decision_at: datetime) -> tuple[WeightTransition, ...]:
        rows = self._conn.execute(
            """
            SELECT symbol, instrument_id, current_weight_before, target_weight_after,
                   delta_weight, gross_notional, allocated_cost, signal_value, signal_rank,
                   signal_period, signal_provenance_json
            FROM portfolio_weight_transitions
            WHERE run_id = ? AND decision_at = ?
            ORDER BY symbol
            """,
            [run_id, decision_at],
        ).fetchall()
        return tuple(
            WeightTransition(
                symbol=str(row[0]),
                instrument_id=str(row[1]),
                current_weight_before=float(row[2]),
                target_weight_after=float(row[3]),
                delta_weight=float(row[4]),
                gross_notional=float(row[5]),
                allocated_cost=float(row[6]),
                signal_value=float(row[7]) if row[7] is not None else None,
                signal_rank=int(row[8]) if row[8] is not None else None,
                signal_period=str(row[9]) if row[9] is not None else None,
                signal_provenance_json=str(row[10]) if row[10] is not None else None,
            )
            for row in rows
        )

    def _load_equity(self, run_id: str) -> tuple[EquityPoint, ...]:
        rows = self._conn.execute(
            """
            SELECT valuation_at, nav, cash_weight, drawdown, benchmark_nav
            FROM portfolio_equity
            WHERE run_id = ?
            ORDER BY valuation_at
            """,
            [run_id],
        ).fetchall()
        return tuple(
            EquityPoint(
                valuation_at=_as_utc(row[0]),
                nav=float(row[1]),
                cash_weight=float(row[2]),
                drawdown=float(row[3]),
                benchmark_nav=float(row[4]) if row[4] is not None else None,
            )
            for row in rows
        )


def normalize_from_request(request: BacktestRequest) -> list[str]:
    from equitytrace.portfolio.signals import normalize_universe

    return normalize_universe(request.tickers)


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"Expected datetime, got {type(value)!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
