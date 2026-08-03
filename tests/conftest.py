"""Shared pytest fixtures for offline FilingEdge tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from filingedge.config import Settings, clear_settings_cache
from filingedge.database import Database, initialize_database
from filingedge.sec.client import (
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
def _clear_settings() -> Iterator[None]:
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings suitable for offline tests."""
    monkeypatch.setenv("FILINGEDGE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("FILINGEDGE_SEC_ORGANIZATION", "FilingEdge Tests")
    monkeypatch.setenv("FILINGEDGE_DATABASE_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setenv("FILINGEDGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FILINGEDGE_ENABLE_CACHE", "false")
    clear_settings_cache()
    return Settings()


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
