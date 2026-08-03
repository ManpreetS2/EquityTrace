"""CLI error-path and smoke tests (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from filingedge.cli import app
from filingedge.config import clear_settings_cache
from filingedge.sec.client import SecClient

runner = CliRunner()


def test_cli_missing_sec_email_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("FILINGEDGE_SEC_EMAIL", raising=False)
    monkeypatch.setenv("FILINGEDGE_SEC_EMAIL", "")
    monkeypatch.setenv("FILINGEDGE_DATABASE_PATH", str(tmp_path / "x.duckdb"))
    clear_settings_cache()

    result = runner.invoke(app, ["resolve", "AAPL"])
    assert result.exit_code != 0
    assert "FILINGEDGE_SEC_EMAIL" in result.output
    assert "Traceback" not in result.output


def test_cli_unknown_ticker_clean_error(
    settings: object,
    sec_mock_transport: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force CLI SecClient construction to use the offline mock transport.
    original_init = SecClient.__init__

    def patched_init(
        self: SecClient,
        settings_obj: object,
        *,
        transport: object | None = None,
        client: object | None = None,
    ) -> None:
        original_init(
            self,
            settings_obj,  # type: ignore[arg-type]
            transport=sec_mock_transport,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(SecClient, "__init__", patched_init)
    result = runner.invoke(app, ["resolve", "ZZZZZ"])
    assert result.exit_code != 0
    assert "Ticker not found" in result.output
    assert "Traceback" not in result.output


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output
