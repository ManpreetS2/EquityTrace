"""CLI tests for statements, factor, factors, and rank commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import initialize_database
from helpers.financial_fixtures import alpha_facts, beta_facts, seed_company

runner = CliRunner()


def _prepare_db(tmp_path: Path, monkeypatch: object) -> Path:
    path = tmp_path / "cli.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")  # type: ignore[attr-defined]
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))  # type: ignore[attr-defined]
    clear_settings_cache()
    db = initialize_database(path)
    with db.session() as conn:
        seed_company(
            conn,
            ticker="ALPHA",
            cik="0001000001",
            legal_name="Alpha Corp",
            facts=alpha_facts(),
        )
        seed_company(
            conn,
            ticker="BETA",
            cik="0001000002",
            legal_name="Beta Inc",
            facts=beta_facts(),
        )
    return path


def test_cli_statements_output(tmp_path: Path, monkeypatch: object) -> None:
    _prepare_db(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["statements", "ALPHA", "--period", "FY2023", "--as-of", "2023-12-01"],
    )
    assert result.exit_code == 0, result.output
    assert "Income statement" in result.output
    assert "revenue" in result.output
    assert "free_cash_flow" in result.output
    assert "Traceback" not in result.output


def test_cli_factor_output(tmp_path: Path, monkeypatch: object) -> None:
    _prepare_db(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "factor",
            "ALPHA",
            "revenue-growth",
            "--period",
            "FY2023",
            "--as-of",
            "2023-12-01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "revenue_growth" in result.output
    assert "Valid" in result.output
    assert "Traceback" not in result.output


def test_cli_factors_output(tmp_path: Path, monkeypatch: object) -> None:
    _prepare_db(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["factors", "ALPHA", "--period", "FY2023", "--as-of", "2023-12-01"],
    )
    assert result.exit_code == 0, result.output
    assert "roa" in result.output
    assert "fcf_yield" in result.output


def test_cli_rank_output(tmp_path: Path, monkeypatch: object) -> None:
    _prepare_db(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "rank",
            "--tickers",
            "ALPHA,BETA",
            "--factor",
            "revenue-growth",
            "--period",
            "FY2023",
            "--as-of",
            "2024-03-01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ALPHA" in result.output
    assert "Percentile" in result.output
    assert "Traceback" not in result.output


def test_cli_unknown_factor_clean_error(tmp_path: Path, monkeypatch: object) -> None:
    _prepare_db(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["factor", "ALPHA", "not-a-factor", "--period", "FY2023"],
    )
    assert result.exit_code != 0
    assert "Unknown factor" in result.output
    assert "Traceback" not in result.output


def test_cli_bad_period_clean_error(tmp_path: Path, monkeypatch: object) -> None:
    _prepare_db(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["statements", "ALPHA", "--period", "YEAR2023"],
    )
    assert result.exit_code != 0
    assert "Unrecognized period" in result.output
    assert "Traceback" not in result.output
