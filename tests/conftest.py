"""Shared pytest fixtures for offline EquityTrace tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from equitytrace.config import Settings, clear_settings_cache
from equitytrace.database import Database, initialize_database
from equitytrace.sec.client import (
    COMPANY_FACTS_URL,
    COMPANY_TICKERS_URL,
    SUBMISSIONS_ARCHIVE_BASE,
    SUBMISSIONS_URL,
    SecClient,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Prevent ambient shell/legacy env from leaking into tests.
    for key in (
        "EQUITYTRACE_SEC_EMAIL",
        "EQUITYTRACE_SEC_ORGANIZATION",
        "EQUITYTRACE_DATABASE_PATH",
        "EQUITYTRACE_CACHE_DIR",
        "EQUITYTRACE_ENABLE_CACHE",
        "EQUITYTRACE_HTTP_TIMEOUT_SECONDS",
        "EQUITYTRACE_MAX_REQUESTS_PER_SECOND",
        "FILINGEDGE_SEC_EMAIL",
        "FILINGEDGE_SEC_ORGANIZATION",
        "FILINGEDGE_DATABASE_PATH",
        "FILINGEDGE_CACHE_DIR",
        "FILINGEDGE_ENABLE_CACHE",
        "FILINGEDGE_HTTP_TIMEOUT_SECONDS",
        "FILINGEDGE_MAX_REQUESTS_PER_SECOND",
    ):
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings suitable for offline tests."""
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_SEC_ORGANIZATION", "EquityTrace Tests")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setenv("EQUITYTRACE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EQUITYTRACE_ENABLE_CACHE", "false")
    clear_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def db(settings: Settings) -> Database:
    """Initialized temporary DuckDB database."""
    return initialize_database(settings.database_path)


@pytest.fixture
def sec_mock_transport() -> httpx.MockTransport:
    """HTTPX mock transport serving reduced SEC-style fixtures."""
    tickers = _load("company_tickers.json")
    submissions = _load("submissions_aapl.json")
    archive = _load("submissions_archive_aapl.json")
    facts = _load("company_facts_aapl.json")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == COMPANY_TICKERS_URL:
            return httpx.Response(200, json=tickers)
        if url == SUBMISSIONS_URL.format(cik="0000320193"):
            return httpx.Response(200, json=submissions)
        if url == f"{SUBMISSIONS_ARCHIVE_BASE}CIK0000320193-submissions-001.json":
            return httpx.Response(200, json=archive)
        if url == COMPANY_FACTS_URL.format(cik="0000320193"):
            return httpx.Response(200, json=facts)
        return httpx.Response(404, text=f"No fixture for {url}")

    return httpx.MockTransport(handler)


@pytest.fixture
def sec_client(settings: Settings, sec_mock_transport: httpx.MockTransport) -> Iterator[SecClient]:
    """SEC client bound to the offline mock transport."""
    with SecClient(settings, transport=sec_mock_transport) as client:
        yield client


@pytest.fixture
def fixture_submissions() -> dict[str, object]:
    return _load("submissions_aapl.json")  # type: ignore[return-value]


@pytest.fixture
def fixture_archive() -> dict[str, object]:
    return _load("submissions_archive_aapl.json")  # type: ignore[return-value]


@pytest.fixture
def fixture_facts() -> dict[str, object]:
    return _load("company_facts_aapl.json")  # type: ignore[return-value]
