"""CLI error-path and smoke tests (offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.sec.client import (
    COMPANY_FACTS_URL,
    COMPANY_TICKERS_URL,
    SUBMISSIONS_ARCHIVE_BASE,
    SUBMISSIONS_URL,
    SecClient,
)

runner = CliRunner()


def _patch_sec_client(
    monkeypatch: pytest.MonkeyPatch,
    mock_transport: httpx.MockTransport,
    *,
    capture_force_refresh: list[bool] | None = None,
) -> None:
    original_init = SecClient.__init__

    def patched_init(
        self: SecClient,
        settings_obj: object,
        *,
        transport: object | None = None,
        client: object | None = None,
        force_refresh: bool = False,
    ) -> None:
        if capture_force_refresh is not None:
            capture_force_refresh.append(force_refresh)
        original_init(
            self,
            settings_obj,  # type: ignore[arg-type]
            transport=mock_transport,
            client=client,  # type: ignore[arg-type]
            force_refresh=force_refresh,
        )

    monkeypatch.setattr(SecClient, "__init__", patched_init)


def test_cli_missing_sec_email_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key in ("EQUITYTRACE_SEC_EMAIL", "FILINGEDGE_SEC_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "x.duckdb"))
    clear_settings_cache()

    result = runner.invoke(app, ["resolve", "AAPL"])
    assert result.exit_code != 0
    assert "EQUITYTRACE_SEC_EMAIL" in result.output
    assert "Traceback" not in result.output


def test_cli_unknown_ticker_clean_error(
    settings: object,
    sec_mock_transport: httpx.MockTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sec_client(monkeypatch, sec_mock_transport)
    result = runner.invoke(app, ["resolve", "ZZZZZ"])
    assert result.exit_code != 0
    assert "Ticker not found" in result.output
    assert "Traceback" not in result.output


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "EquityTrace" in result.output
    assert "1.0.0" in result.output


def test_cli_help_branding() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "EquityTrace" in result.output
    assert "filings" in result.output.lower() or "factors" in result.output.lower()


def test_cli_ingest_refresh_flag_reaches_sec_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sec_mock_transport: httpx.MockTransport,
) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_SEC_ORGANIZATION", "EquityTrace Tests")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "ingest.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    clear_settings_cache()

    captured: list[bool] = []
    _patch_sec_client(monkeypatch, sec_mock_transport, capture_force_refresh=captured)

    result = runner.invoke(
        app,
        ["ingest", "AAPL", "--database", str(tmp_path / "ingest.duckdb"), "--refresh"],
    )
    assert result.exit_code == 0, result.output
    assert captured == [True]
    assert "SEC evidence" in result.output
    assert "--refresh" in result.output


def test_cli_ingest_without_refresh_does_not_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sec_mock_transport: httpx.MockTransport,
) -> None:
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_SEC_ORGANIZATION", "EquityTrace Tests")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "ingest2.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache2"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    clear_settings_cache()

    captured: list[bool] = []
    _patch_sec_client(monkeypatch, sec_mock_transport, capture_force_refresh=captured)

    result = runner.invoke(
        app,
        ["ingest", "AAPL", "--database", str(tmp_path / "ingest2.duckdb")],
    )
    assert result.exit_code == 0, result.output
    assert captured == [False]
    assert "SEC evidence" in result.output


def test_cli_ingest_refresh_triggers_network_on_second_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With cache enabled, --refresh must hit the network again for SEC endpoints."""
    fixtures = Path(__file__).parent / "fixtures" / "sec"
    tickers = json.loads((fixtures / "company_tickers.json").read_text())
    submissions = json.loads((fixtures / "submissions_aapl.json").read_text())
    archive = json.loads((fixtures / "submissions_archive_aapl.json").read_text())
    facts = json.loads((fixtures / "company_facts_aapl.json").read_text())
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        hits.append(url)
        if url == COMPANY_TICKERS_URL:
            return httpx.Response(200, json=tickers)
        if url == SUBMISSIONS_URL.format(cik="0000320193"):
            return httpx.Response(200, json=submissions)
        if url == f"{SUBMISSIONS_ARCHIVE_BASE}CIK0000320193-submissions-001.json":
            return httpx.Response(200, json=archive)
        if url == COMPANY_FACTS_URL.format(cik="0000320193"):
            return httpx.Response(200, json=facts)
        return httpx.Response(404, text=url)

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_SEC_ORGANIZATION", "EquityTrace Tests")
    db_path = tmp_path / "refresh.duckdb"
    cache_dir = tmp_path / "cache-refresh"
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "true")
    monkeypatch.setenv("EQUITYTRACE_SEC_CACHE_TTL_SECONDS", "86400")
    clear_settings_cache()
    _patch_sec_client(monkeypatch, transport)

    first = runner.invoke(app, ["ingest", "AAPL", "--database", str(db_path)])
    assert first.exit_code == 0, first.output
    first_count = len(hits)
    assert first_count >= 3  # tickers + submissions (+ archive) + facts

    hits.clear()
    second = runner.invoke(app, ["ingest", "AAPL", "--database", str(db_path)])
    assert second.exit_code == 0, second.output
    # Unexpired cache: endpoints should not re-hit the network.
    assert len(hits) == 0

    third = runner.invoke(app, ["ingest", "AAPL", "--database", str(db_path), "--refresh"])
    assert third.exit_code == 0, third.output
    assert len(hits) >= 3
    assert any("company_tickers" in u for u in hits)
    assert any("submissions" in u for u in hits)
    assert any("companyfacts" in u for u in hits)
