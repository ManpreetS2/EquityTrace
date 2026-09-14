"""CLI smoke tests for equitytrace market commands."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.market.models import PriceAdjustmentMode
from equitytrace.market.providers.twelve_data import TwelveDataProvider
from equitytrace.market.service import MarketDataService

runner = CliRunner()


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cli_market.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    clear_settings_cache()
    initialize_database(path)
    return path


def test_market_help() -> None:
    result = runner.invoke(app, ["market", "--help"])
    assert result.exit_code == 0
    assert "ingest" in result.output
    assert "cap" in result.output
    assert "analytics" in result.output
    assert "rank-metric" in result.output


def test_missing_api_key_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "x.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.delenv("EQUITYTRACE_TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.setenv("EQUITYTRACE_TWELVE_DATA_API_KEY", "")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    clear_settings_cache()
    result = runner.invoke(
        app,
        ["market", "ingest", "AAPL", "--start", "2023-01-01", "--end", "2023-01-05"],
    )
    assert result.exit_code != 0
    assert "EQUITYTRACE_TWELVE_DATA_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_ingest_prices_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _prepare(tmp_path, monkeypatch)
    payload = {
        "meta": {"currency": "USD", "exchange_timezone": "America/New_York"},
        "status": "ok",
        "values": [
            {
                "datetime": "2023-01-03",
                "open": "100",
                "high": "110",
                "low": "95",
                "close": "105",
                "volume": "1000",
            }
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    # Seed via service with mocked provider (CLI constructs its own provider).
    from equitytrace.config import Settings

    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    db = initialize_database(path)
    with db.session() as conn:
        provider = TwelveDataProvider(settings, transport=transport)
        MarketDataService(conn, settings, provider=provider).ingest(
            "AAPL",
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 10),
            modes=[PriceAdjustmentMode.NONE, PriceAdjustmentMode.ALL],
        )

    prices = runner.invoke(
        app,
        [
            "market",
            "prices",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-01-10",
            "--adjustment",
            "none",
        ],
    )
    assert prices.exit_code == 0
    assert "105" in prices.output

    prices_json = runner.invoke(
        app,
        [
            "market",
            "prices",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-01-10",
            "--adjustment",
            "all",
            "--format",
            "json",
        ],
    )
    assert prices_json.exit_code == 0
    assert "available_at" in prices_json.output
    assert "fetched_at" in prices_json.output


def test_existing_sec_commands_still_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "x.duckdb"))
    # No market key required for version/help.
    monkeypatch.delenv("EQUITYTRACE_TWELVE_DATA_API_KEY", raising=False)
    clear_settings_cache()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.3.1.dev0" in result.output


def test_market_analytics_unavailable_is_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["market", "analytics", "AAPL", "--as-of", "2024-12-31"],
    )
    assert result.exit_code == 0, result.output
    assert "momentum_12_1" in result.output
    assert "beta_1y" in result.output
    assert "Traceback" not in result.output


def test_rank_metric_refuses_beta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["market", "rank-metric", "beta_1y", "AAPL", "MSFT", "--as-of", "2024-12-31"],
    )
    assert result.exit_code != 0
    assert "ranking direction" in result.output.lower()
    assert "Traceback" not in result.output
