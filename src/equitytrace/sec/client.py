"""Reusable SEC EDGAR HTTP client with rate limiting, retries, and caching."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from equitytrace.config import Settings

logger = logging.getLogger(__name__)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_ARCHIVE_BASE = "https://data.sec.gov/submissions/"


class SecClientError(RuntimeError):
    """Base error for SEC client failures."""


class SecHttpError(SecClientError):
    """HTTP error returned by an SEC endpoint."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class RetryableSecError(SecHttpError):
    """Transient SEC HTTP/transport error that should be retried."""


class _RateLimiter:
    """Simple thread-safe token-bucket style rate limiter."""

    def __init__(self, max_requests_per_second: float) -> None:
        self._min_interval = 1.0 / max_requests_per_second
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request_at = time.monotonic()


class SecClient:
    """HTTP client for official SEC EDGAR JSON endpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._rate_limiter = _RateLimiter(settings.max_requests_per_second)
        self._owns_client = client is None
        headers = {
            "User-Agent": settings.user_agent(),
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        }
        self._client = client or httpx.Client(
            headers=headers,
            timeout=settings.http_timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )
        if settings.enable_cache:
            settings.cache_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Close the underlying HTTP client if owned by this instance."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def user_agent(self) -> str:
        """Return the configured User-Agent header value."""
        return self._settings.user_agent()

    def get_json(self, url: str, *, use_cache: bool | None = None) -> Any:
        """GET a JSON payload with rate limiting, retries, and optional caching."""
        cache_enabled = self._settings.enable_cache if use_cache is None else use_cache
        if cache_enabled:
            cached = self._read_cache(url)
            if cached is not None:
                logger.debug("Cache hit for %s", url)
                return cached

        payload = self._get_json_with_retry(url)
        if cache_enabled:
            self._write_cache(url, payload)
        return payload

    def get_company_tickers(self) -> dict[str, Any]:
        """Fetch the SEC company tickers mapping."""
        data = self.get_json(COMPANY_TICKERS_URL)
        if not isinstance(data, dict):
            raise SecClientError("Unexpected company_tickers.json payload shape")
        return data

    def get_submissions(self, cik: str) -> dict[str, Any]:
        """Fetch submissions metadata for a CIK."""
        url = SUBMISSIONS_URL.format(cik=cik)
        data = self.get_json(url)
        if not isinstance(data, dict):
            raise SecClientError(f"Unexpected submissions payload for CIK {cik}")
        return data

    def get_archived_submissions(self, filename: str) -> dict[str, Any]:
        """Fetch an archived submissions file referenced by the main response."""
        url = f"{SUBMISSIONS_ARCHIVE_BASE}{filename.lstrip('/')}"
        data = self.get_json(url)
        if not isinstance(data, dict):
            raise SecClientError(f"Unexpected archived submissions payload: {filename}")
        return data

    def get_company_facts(self, cik: str) -> dict[str, Any]:
        """Fetch XBRL Company Facts for a CIK."""
        url = COMPANY_FACTS_URL.format(cik=cik)
        data = self.get_json(url)
        if not isinstance(data, dict):
            raise SecClientError(f"Unexpected company facts payload for CIK {cik}")
        return data

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(RetryableSecError),
    )
    def _get_json_with_retry(self, url: str) -> Any:
        self._rate_limiter.wait()
        logger.debug("GET %s", url)
        try:
            response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise RetryableSecError(f"Timeout requesting {url}", url=url) from exc
        except httpx.TransportError as exc:
            raise RetryableSecError(f"Transport error requesting {url}: {exc}", url=url) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableSecError(
                f"Retryable SEC HTTP {response.status_code} for {url}",
                status_code=response.status_code,
                url=url,
            )
        if response.status_code >= 400:
            raise SecHttpError(
                f"SEC HTTP {response.status_code} for {url}: {response.text[:200]}",
                status_code=response.status_code,
                url=url,
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise SecClientError(f"Invalid JSON from {url}") from exc

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._settings.cache_dir / f"{digest}.json"

    def _read_cache(self, url: str) -> Any | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring corrupt cache file %s", path)
            return None

    def _write_cache(self, url: str, payload: Any) -> None:
        path = self._cache_path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            logger.warning("Failed to write cache file %s", path)
