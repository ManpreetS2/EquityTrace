"""Rename and backward-compatibility tests for EquityTrace branding."""

from __future__ import annotations

import importlib
import warnings
from pathlib import Path

import pytest
from typer.testing import CliRunner

from equitytrace.cli import app, deprecated_filingedge_cli
from equitytrace.config import (
    ConfigurationError,
    LegacyEnvVarWarning,
    Settings,
    clear_settings_cache,
    emit_legacy_env_warnings,
)
from equitytrace.database import initialize_database

runner = CliRunner()

_LEGACY_KEYS = (
    "FILINGEDGE_SEC_EMAIL",
    "FILINGEDGE_SEC_ORGANIZATION",
    "FILINGEDGE_DATABASE_PATH",
    "FILINGEDGE_CACHE_DIR",
    "FILINGEDGE_ENABLE_CACHE",
    "FILINGEDGE_HTTP_TIMEOUT_SECONDS",
    "FILINGEDGE_MAX_REQUESTS_PER_SECOND",
)
_NEW_KEYS = (
    "EQUITYTRACE_SEC_EMAIL",
    "EQUITYTRACE_SEC_ORGANIZATION",
    "EQUITYTRACE_DATABASE_PATH",
    "EQUITYTRACE_CACHE_DIR",
    "EQUITYTRACE_ENABLE_CACHE",
    "EQUITYTRACE_HTTP_TIMEOUT_SECONDS",
    "EQUITYTRACE_MAX_REQUESTS_PER_SECOND",
)


def _clear_all_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*_NEW_KEYS, *_LEGACY_KEYS):
        monkeypatch.delenv(key, raising=False)


def test_default_database_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_all_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_path == Path("data/equitytrace.duckdb")


def test_new_env_variables_work(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_all_env(monkeypatch)
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "new@example.com")
    monkeypatch.setenv("EQUITYTRACE_SEC_ORGANIZATION", "New Org")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "new.duckdb"))
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.sec_email == "new@example.com"
    assert settings.sec_organization == "New Org"
    assert settings.database_path == tmp_path / "new.duckdb"


def test_legacy_env_variables_still_work(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_all_env(monkeypatch)
    monkeypatch.setenv("FILINGEDGE_SEC_EMAIL", "legacy@example.com")
    monkeypatch.setenv("FILINGEDGE_DATABASE_PATH", str(tmp_path / "legacy.duckdb"))
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.sec_email == "legacy@example.com"
    assert settings.database_path == tmp_path / "legacy.duckdb"


def test_new_env_overrides_legacy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_all_env(monkeypatch)
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "winner@example.com")
    monkeypatch.setenv("FILINGEDGE_SEC_EMAIL", "loser@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "winner.duckdb"))
    monkeypatch.setenv("FILINGEDGE_DATABASE_PATH", str(tmp_path / "loser.duckdb"))
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.sec_email == "winner@example.com"
    assert settings.database_path == tmp_path / "winner.duckdb"


def test_missing_sec_email_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_env(monkeypatch)
    clear_settings_cache()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError, match="EQUITYTRACE_SEC_EMAIL") as exc:
        settings.require_sec_email()
    assert "secret" not in str(exc.value).lower()


def test_legacy_env_warning_omits_secret_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_all_env(monkeypatch)
    secret = "super-secret-contact@example.com"
    monkeypatch.setenv("FILINGEDGE_SEC_EMAIL", secret)
    monkeypatch.chdir(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", LegacyEnvVarWarning)
        warned = emit_legacy_env_warnings(env_file=tmp_path / "missing.env")
    assert "FILINGEDGE_SEC_EMAIL" in warned
    assert caught
    joined = " | ".join(str(item.message) for item in caught)
    assert secret not in joined
    assert "FILINGEDGE_SEC_EMAIL" in joined
    assert "EQUITYTRACE_SEC_EMAIL" in joined


def test_no_legacy_warning_when_new_var_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_all_env(monkeypatch)
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "new@example.com")
    monkeypatch.setenv("FILINGEDGE_SEC_EMAIL", "legacy@example.com")
    monkeypatch.chdir(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", LegacyEnvVarWarning)
        warned = emit_legacy_env_warnings(env_file=tmp_path / "missing.env")
    assert warned == []
    assert not any(isinstance(item.message, LegacyEnvVarWarning) for item in caught)


def test_v01_database_still_migrates(tmp_path: Path) -> None:
    """A freshly initialized path remains readable after EquityTrace init."""
    path = tmp_path / "legacy_name_filingedge.duckdb"
    db = initialize_database(path)
    with db.session() as conn:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert "issuers" in tables
    assert "financial_facts" in tables
    assert "factor_runs" in tables
    assert path.exists()


def test_deprecated_cli_alias_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[int] = []

    def fake_app() -> None:
        calls.append(1)

    monkeypatch.setattr("equitytrace.cli.app", fake_app)
    deprecated_filingedge_cli()
    assert calls == [1]
    err = capsys.readouterr().err
    assert "DeprecationWarning" in err
    assert "equitytrace" in err.lower()
    assert "removed" in err.lower()
    assert "secret" not in err.lower()


def test_primary_cli_help_is_equitytrace() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "EquityTrace" in result.output


def test_no_source_imports_filingedge() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "from filingedge" in line or "import filingedge" in line:
                offenders.append(f"{path}:{line_no}:{line.strip()}")
    assert offenders == []


def test_package_importable() -> None:
    mod = importlib.import_module("equitytrace")
    assert hasattr(mod, "__version__")
    assert mod.__version__.startswith("0.3")


def test_docs_do_not_present_filingedge_as_current_name() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked_docs = [
        root / "README.md",
        root / "START_HERE.md",
        root / "docs" / "architecture.md",
        root / "docs" / "data-model.md",
        root / "docs" / "roadmap.md",
    ]
    for path in tracked_docs:
        text = path.read_text(encoding="utf-8")
        # Historical mentions are allowed when clearly about the rename / legacy.
        # Current-product headings must use EquityTrace.
        if path.name == "README.md":
            assert text.lstrip().startswith("# EquityTrace")
        assert "uv run filingedge" not in text
        assert "`filingedge init-db`" not in text
