"""Atomic persistence round-trips for portfolio runs."""

from __future__ import annotations

import pytest

from equitytrace.database import Database
from equitytrace.portfolio.models import PortfolioRunStatus, RebalanceStatus
from equitytrace.repositories.portfolio import PortfolioRepository
from helpers.portfolio_fixtures import explicit_request, run_backtest
from test_portfolio_backtest import D0, D1, D3, _seed_two_name_path


def test_success_run_roundtrips(db: Database) -> None:
    _seed_two_name_path(db)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1)),
    )
    assert result.status is PortfolioRunStatus.SUCCESS
    with db.session() as conn:
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert loaded.status is PortfolioRunStatus.SUCCESS
    assert loaded.final_nav == pytest.approx(result.final_nav or 0.0)
    assert len(loaded.rebalances) == len(result.rebalances)
    assert loaded.rebalances[0].decision_at == result.rebalances[0].decision_at
    assert loaded.rebalances[0].transitions[0].signal_period == "FY2023"
    assert loaded.warnings == result.warnings
    assert loaded.audit is not None


def test_injected_write_failure_does_not_leave_success_row(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_name_path(db)
    request = explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1))

    original = PortfolioRepository.persist_run

    def boom(self: PortfolioRepository, result: object) -> None:
        self._conn.execute("BEGIN TRANSACTION")
        self._conn.execute(
            "INSERT INTO portfolio_runs (run_id, created_at, status, start_date, end_date, "
            "schedule, calendar_symbol, factor_name, top_n, baseline, cost_bps, initial_nav, "
            "provider, adjustment_mode, package_version) VALUES (?, CURRENT_TIMESTAMP, 'success', "
            "DATE '2024-01-01', DATE '2024-01-05', 'explicit', 'SPY', 'roa', 2, 'equal_weight', "
            "10, 1.0, 'twelve_data', 'all', 'x')",
            ["partial"],
        )
        raise RuntimeError("injected")

    monkeypatch.setattr(PortfolioRepository, "persist_run", boom)
    result = run_backtest(db, request)
    assert result.status is PortfolioRunStatus.FAILED
    assert result.failure_reason == "database_error"
    with db.session() as conn:
        rows = conn.execute("SELECT status FROM portfolio_runs").fetchall()
    assert rows == []
    monkeypatch.setattr(PortfolioRepository, "persist_run", original)


def test_duckdb_error_during_execution_is_database_error(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import duckdb

    from equitytrace.repositories.market import MarketRepository

    _seed_two_name_path(db)

    def boom(self: MarketRepository, *args: object, **kwargs: object) -> list[object]:
        raise duckdb.Error("injected read failure")

    monkeypatch.setattr(MarketRepository, "get_price_bars_for_instruments", boom)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1)),
    )
    assert result.status is PortfolioRunStatus.FAILED
    assert result.failure_reason == "database_error"
    with db.session() as conn:
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert loaded.status is PortfolioRunStatus.FAILED
    assert loaded.failure_reason == "database_error"


def test_unavailable_later_rebalance_has_zero_cost_rows(db: Database) -> None:
    from datetime import UTC, datetime

    from helpers.portfolio_fixtures import add_adjusted_bars, annual_roa_facts, seed_issuer
    from test_portfolio_backtest import D2, KNOWN

    days = [D0, D1, D2, D3]
    late = datetime(2024, 1, 3, 12, 0, tzinfo=UTC)
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=5.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        seed_issuer(
            conn,
            ticker="CCC",
            cik="0001000003",
            facts=annual_roa_facts(
                cik="0001000003",
                fiscal_year=2023,
                net_income=50.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=late,
                accession="ccc-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "CCC", [(day, 100.0) for day in days], skip_dates={D2})
    result = run_backtest(
        db,
        explicit_request(("AAA", "CCC"), start=D0, end=D3, decisions=(D0, D1), top_n=1),
    )
    later = [row for row in result.rebalances if row.status is RebalanceStatus.UNAVAILABLE]
    assert later
    assert later[0].transitions == ()
    assert later[0].cost_amount == 0.0
