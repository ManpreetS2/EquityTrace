"""CLI coverage for `equitytrace research validate|run|show`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import Database, initialize_database
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.portfolio.models import (
    BacktestRequest,
    PortfolioBaseline,
    PortfolioRunStatus,
    PortfolioSchedule,
)
from equitytrace.repositories.portfolio import PortfolioRepository
from equitytrace.strategy.io import load_strategy
from equitytrace.strategy.models import fingerprint_request, fingerprint_strategy
from test_portfolio_backtest import D0, D1, D2, D3, _seed_two_name_path

runner = CliRunner()


def _write_strategy(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "name": "Toy research",
        "description": "test only",
        "tickers": ["BBB", "AAA"],
        "factor": "roa",
        "top_n": 2,
        "baseline": "equal_weight",
        "start_date": D0.isoformat(),
        "end_date": D3.isoformat(),
        "schedule": "explicit",
        "explicit_dates": [D1.isoformat(), D0.isoformat()],
        "calendar_symbol": "SPY",
        "benchmark_symbol": "SPY",
        "cost_bps": 10.0,
        "initial_nav": 1.0,
        "provider": "twelve_data",
        "adjustment_mode": "all",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _prepare(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "research.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(db_path))
    clear_settings_cache()
    db = initialize_database(db_path)
    _seed_two_name_path(db)
    strategy = _write_strategy(tmp_path / "strategy.json")
    return db_path, strategy


def test_research_help_lists_commands() -> None:
    result = runner.invoke(app, ["research", "--help"])
    assert result.exit_code == 0
    assert "validate" in result.output
    assert "run" in result.output
    assert "show" in result.output
    assert "report" in result.output
    assert "export" in result.output


def test_portfolio_backtest_still_registered() -> None:
    result = runner.invoke(app, ["portfolio", "--help"])
    assert result.exit_code == 0
    assert "backtest" in result.output


def test_validate_success_no_database(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _refuse_database(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("validate must not open DuckDB")

    monkeypatch.setattr("duckdb.connect", _refuse_database)
    path = _write_strategy(tmp_path / "ok.json")
    result = runner.invoke(app, ["research", "validate", "--strategy", str(path)])
    assert result.exit_code == 0
    strategy = load_strategy(path)
    assert fingerprint_strategy(strategy) in result.output
    assert "AAA, BBB" in result.output
    assert "roa" in result.output
    assert "higher_is_better" in result.output
    assert "Toy research" in result.output


def test_validate_and_run_reject_unsafe_json_without_traceback(tmp_path: Path) -> None:
    files = {
        "utf8.json": None,
        "dup-root.json": (
            '{"name": "Toy research", "factor": "roa", "factor": "fcf_yield", '
            '"tickers": ["AAA"], "top_n": 1, "baseline": "equal_weight", '
            '"start_date": "2024-01-02", "end_date": "2024-01-31", "schedule": "monthly"}'
        ),
        "dup-nested.json": (
            '{"name": "Toy research", "tickers": ["AAA"], "factor": "roa", "top_n": 1, '
            '"baseline": "equal_weight", "start_date": "2024-01-02", '
            '"end_date": "2024-01-31", "schedule": "monthly", "notes": {"a": 1, "a": 2}}'
        ),
        "malformed.json": "{",
    }
    utf8 = tmp_path / "utf8.json"
    utf8.write_bytes(b'{"name": "\xff"}')
    paths = [utf8]
    for name, text in files.items():
        if text is None:
            continue
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    for path in paths:
        for command in ("validate", "run"):
            result = runner.invoke(app, ["research", command, "--strategy", str(path)])
            assert result.exit_code == 1
            assert "Traceback" not in result.output
            assert "Error:" in result.output


def test_validate_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    result = runner.invoke(app, ["research", "validate", "--strategy", str(path)])
    assert result.exit_code == 1
    assert "Invalid JSON" in result.output


def test_validate_unknown_field(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path / "extra.json", mystery=True)
    result = runner.invoke(app, ["research", "validate", "--strategy", str(path)])
    assert result.exit_code == 1
    assert "Invalid strategy" in result.output


def test_validate_unknown_factor(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path / "factor.json", factor="not-a-factor")
    result = runner.invoke(app, ["research", "validate", "--strategy", str(path)])
    assert result.exit_code == 1
    assert "Unknown factor" in result.output or "Invalid strategy" in result.output


def test_run_success_persists_canonical_request(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path, strategy_path = _prepare(tmp_path, monkeypatch)
    strategy = load_strategy(strategy_path)
    expected = fingerprint_strategy(strategy)
    result = runner.invoke(
        app,
        ["research", "run", "--strategy", str(strategy_path), "--database", str(db_path)],
    )
    assert result.exit_code == 0
    assert expected in result.output
    assert "package_version" in result.output or "1.0.0.dev0" in result.output
    assert "run_id=" in result.output
    assert "Final NAV" in result.output
    db = initialize_database(db_path)
    with db.session() as conn:
        run_id = conn.execute("SELECT run_id FROM portfolio_runs").fetchone()
        assert run_id is not None
        loaded = PortfolioRepository(conn).get_run(str(run_id[0]))
    assert loaded is not None
    assert loaded.status is PortfolioRunStatus.SUCCESS
    assert loaded.request.tickers == ("AAA", "BBB")
    assert loaded.request.explicit_dates == (D0, D1)
    assert fingerprint_request(loaded.request) == expected


def test_run_unavailable_exit_2(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from helpers.portfolio_fixtures import add_adjusted_bars, annual_roa_facts, seed_issuer
    from test_portfolio_backtest import KNOWN

    db_path = tmp_path / "unavail.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(db_path))
    clear_settings_cache()
    db = initialize_database(db_path)
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "BBB", [(day, 100.0) for day in days])
    strategy_path = _write_strategy(
        tmp_path / "partial.json",
        tickers=["AAA", "BBB"],
        top_n=2,
        explicit_dates=[D0.isoformat()],
    )
    result = runner.invoke(
        app,
        ["research", "run", "--strategy", str(strategy_path), "--database", str(db_path)],
    )
    assert result.exit_code == 2, result.output
    assert "unavailable" in result.output.lower() or "status=unavailable" in result.output


def test_run_failed_exit_1(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "fail.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(db_path))
    clear_settings_cache()
    db = initialize_database(db_path)
    _seed_two_name_path(db, skip_bbb=D2)
    strategy_path = _write_strategy(
        tmp_path / "fail.json",
        explicit_dates=[D0.isoformat()],
    )
    result = runner.invoke(
        app,
        ["research", "run", "--strategy", str(strategy_path), "--database", str(db_path)],
    )
    assert result.exit_code == 1
    assert "failed" in result.output.lower() or "status=failed" in result.output


def test_show_success_and_fingerprint_roundtrip(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path, strategy_path = _prepare(tmp_path, monkeypatch)
    expected = fingerprint_strategy(load_strategy(strategy_path))
    run = runner.invoke(
        app,
        ["research", "run", "--strategy", str(strategy_path), "--database", str(db_path)],
    )
    assert run.exit_code == 0
    db = initialize_database(db_path)
    with db.session() as conn:
        run_id = str(conn.execute("SELECT run_id FROM portfolio_runs").fetchone()[0])
    opened: list[bool] = []
    real_connect = Database.connect

    def _track_connect(self: Database, *, read_only: bool = False) -> object:
        opened.append(read_only)
        return real_connect(self, read_only=read_only)

    monkeypatch.setattr(Database, "connect", _track_connect)
    shown = runner.invoke(
        app,
        ["research", "show", run_id, "--database", str(db_path)],
    )
    assert shown.exit_code == 0
    assert expected in shown.output
    assert "1.0.0.dev0" in shown.output
    assert "provider_adjusted_history_not_vintage_pit" in shown.output
    assert "audit_passed" in shown.output
    assert "rebalance_count" in shown.output
    assert "equity_point_count" in shown.output
    assert opened == [True]


def test_show_unavailable_and_failed_exit_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from helpers.portfolio_fixtures import add_adjusted_bars, annual_roa_facts, seed_issuer
    from test_portfolio_backtest import KNOWN

    db_path = tmp_path / "show_unavail.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(db_path))
    clear_settings_cache()
    db = initialize_database(db_path)
    days = [D0, D1, D2, D3]
    with db.session() as conn:
        seed_issuer(
            conn,
            ticker="AAA",
            cik="0001000001",
            facts=annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=20.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="aaa-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "BBB", [(day, 100.0) for day in days])
    unavailable = _write_strategy(
        tmp_path / "unavail.json",
        tickers=["AAA", "BBB"],
        top_n=2,
        explicit_dates=[D0.isoformat()],
    )
    unavail_run = runner.invoke(
        app,
        ["research", "run", "--strategy", str(unavailable), "--database", str(db_path)],
    )
    assert unavail_run.exit_code == 2, unavail_run.output
    with db.session() as conn:
        run_id = str(
            conn.execute(
                "SELECT run_id FROM portfolio_runs WHERE status = 'unavailable' LIMIT 1"
            ).fetchone()[0]
        )
    shown = runner.invoke(app, ["research", "show", run_id, "--database", str(db_path)])
    assert shown.exit_code == 0
    assert "unavailable" in shown.output

    fail_db = tmp_path / "fail2.duckdb"
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(fail_db))
    clear_settings_cache()
    db2 = initialize_database(fail_db)
    _seed_two_name_path(db2, skip_bbb=D2)
    fail_strategy = _write_strategy(
        tmp_path / "fail2.json",
        explicit_dates=[D0.isoformat()],
    )
    fail_run = runner.invoke(
        app,
        ["research", "run", "--strategy", str(fail_strategy), "--database", str(fail_db)],
    )
    assert fail_run.exit_code == 1
    with db2.session() as conn:
        fail_id = str(
            conn.execute(
                "SELECT run_id FROM portfolio_runs WHERE status = 'failed' LIMIT 1"
            ).fetchone()[0]
        )
    fail_show = runner.invoke(app, ["research", "show", fail_id, "--database", str(fail_db)])
    assert fail_show.exit_code == 0
    assert "failed" in fail_show.output
    assert "audit_passed" in fail_show.output
    assert "audit_failures" in fail_show.output


def test_show_missing_run_exit_1(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path, _ = _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["research", "show", "does-not-exist", "--database", str(db_path)],
    )
    assert result.exit_code == 1
    assert "No portfolio run found" in result.output


def test_legacy_loaded_request_fingerprint_is_canonical(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    db_path, _strategy_path = _prepare(tmp_path, monkeypatch)
    request = BacktestRequest(
        tickers=("bbb", " AAA ", "aaa"),
        factor="ROA",
        top_n=2,
        baseline=PortfolioBaseline.EQUAL_WEIGHT,
        start_date=D0,
        end_date=D3,
        schedule=PortfolioSchedule.EXPLICIT,
        explicit_dates=(D1, D0, D0),
        calendar_symbol="spy",
        benchmark_symbol=" spy ",
    )
    expected = fingerprint_strategy(
        load_strategy(
            _write_strategy(
                tmp_path / "canonical.json",
                tickers=["AAA", "BBB"],
                factor="roa",
                explicit_dates=[D0.isoformat(), D1.isoformat()],
            )
        )
    )
    db = initialize_database(db_path)
    with db.session() as conn:
        result = BacktestEngine(conn).run(request)
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert loaded.request.factor == "ROA"
    assert loaded.request.tickers == ("bbb", " AAA ", "aaa")
    assert fingerprint_request(loaded.request) == expected


def test_persistence_roundtrip_fingerprint(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path, strategy_path = _prepare(tmp_path, monkeypatch)
    strategy = load_strategy(strategy_path)
    request = strategy.to_backtest_request()
    expected = fingerprint_strategy(strategy)
    db = initialize_database(db_path)
    with db.session() as conn:
        result = BacktestEngine(conn).run(request)
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert fingerprint_request(loaded.request) == expected
