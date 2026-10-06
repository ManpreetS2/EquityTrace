"""CLI coverage for `equitytrace portfolio backtest`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import initialize_database
from test_portfolio_backtest import D0, D1, D3, _seed_two_name_path

runner = CliRunner()


def _prepare(tmp_path: Path, monkeypatch) -> Path:  # type: ignore[no-untyped-def]
    path = tmp_path / "p.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    clear_settings_cache()
    db = initialize_database(path)
    _seed_two_name_path(db)
    return path


def test_portfolio_help() -> None:
    result = runner.invoke(app, ["portfolio", "--help"])
    assert result.exit_code == 0
    assert "backtest" in result.output
    backtest_help = runner.invoke(app, ["portfolio", "backtest", "--help"])
    assert backtest_help.exit_code == 0
    assert "--adjustment-mode" not in backtest_help.output
    assert "--adjustment" not in backtest_help.output


def test_version_still_dev_line() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.3.2.dev0" in result.output


def test_successful_toy_backtest(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "portfolio",
            "backtest",
            "--tickers",
            "AAA,BBB",
            "--factor",
            "roa",
            "--top-n",
            "2",
            "--baseline",
            "equal-weight",
            "--start",
            D0.isoformat(),
            "--end",
            D3.isoformat(),
            "--schedule",
            "explicit",
            "--explicit-dates",
            f"{D0.isoformat()},{D1.isoformat()}",
        ],
    )
    assert result.exit_code == 0
    assert "Final NAV" in result.output
    assert "provider_adjusted_history_not_vintage_pit" in result.output
    db = initialize_database(path)
    with db.session() as conn:
        mode = conn.execute("SELECT adjustment_mode FROM portfolio_runs").fetchone()
    assert mode is not None
    assert mode[0] == "all"


def test_unavailable_first_rebalance(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "portfolio",
            "backtest",
            "--tickers",
            "AAA",
            "--factor",
            "roa",
            "--top-n",
            "2",
            "--start",
            D0.isoformat(),
            "--end",
            D3.isoformat(),
            "--schedule",
            "explicit",
            "--explicit-dates",
            D0.isoformat(),
        ],
    )
    assert result.exit_code == 2
    assert "factor_universe_too_small" in result.output


def test_failed_missing_held_return(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from helpers.portfolio_fixtures import add_adjusted_bars, annual_roa_facts, seed_issuer
    from test_portfolio_backtest import D2, KNOWN

    path = tmp_path / "fail.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    clear_settings_cache()
    db = initialize_database(path)
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
        seed_issuer(
            conn,
            ticker="BBB",
            cik="0001000002",
            facts=annual_roa_facts(
                cik="0001000002",
                fiscal_year=2023,
                net_income=10.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=KNOWN,
                accession="bbb-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "AAA", [(day, 100.0) for day in days])
        add_adjusted_bars(conn, "BBB", [(day, 100.0) for day in days], skip_dates={D2})
    result = runner.invoke(
        app,
        [
            "portfolio",
            "backtest",
            "--tickers",
            "AAA,BBB",
            "--factor",
            "roa",
            "--top-n",
            "2",
            "--start",
            D0.isoformat(),
            "--end",
            D3.isoformat(),
            "--schedule",
            "explicit",
            "--explicit-dates",
            D0.isoformat(),
        ],
    )
    assert result.exit_code == 1
    assert "held_return_missing" in result.output


def test_invalid_factor(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "portfolio",
            "backtest",
            "--tickers",
            "AAA",
            "--factor",
            "not-a-factor",
            "--top-n",
            "1",
            "--start",
            D0.isoformat(),
            "--end",
            D3.isoformat(),
            "--schedule",
            "explicit",
            "--explicit-dates",
            D0.isoformat(),
        ],
    )
    assert result.exit_code != 0


def test_min_variance_baseline_alias(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "portfolio",
            "backtest",
            "--tickers",
            "AAA,BBB",
            "--factor",
            "roa",
            "--top-n",
            "2",
            "--baseline",
            "min-variance",
            "--start",
            D0.isoformat(),
            "--end",
            D3.isoformat(),
            "--schedule",
            "explicit",
            "--explicit-dates",
            D0.isoformat(),
        ],
    )
    assert result.exit_code == 2
    assert "baseline_unavailable" in result.output


def test_invalid_top_n(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "portfolio",
            "backtest",
            "--tickers",
            "AAA",
            "--factor",
            "roa",
            "--top-n",
            "0",
            "--start",
            D0.isoformat(),
            "--end",
            D3.isoformat(),
        ],
    )
    assert result.exit_code != 0
