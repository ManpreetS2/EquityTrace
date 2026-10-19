"""Reusable SEC EDGAR HTTP client with rate limiting, retries, and caching."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
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
_ARCHIVE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.json$")


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


@dataclass
class CacheAccessStats:
    """Observed cache vs network evidence for this client instance.

    Counts reflect actual access decisions. Ages are never invented.
    """

    cache_hits: int = 0
    network_fetches: int = 0
    expired_skips: int = 0
    forced_refreshes: int = 0
    urls_from_cache: list[str] = field(default_factory=list)
    urls_fetched: list[str] = field(default_factory=list)


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
    """HTTP client for official SEC EDGAR JSON endpoints.

    Cache freshness policy
    ----------------------
    * **Mutable endpoints** (``company_tickers``, current submissions,
      Company Facts, and any other non-archive URL): cached responses are
      reused only while younger than ``Settings.sec_cache_ttl_seconds``
      (default 24 hours). Expired entries are never returned; a network
      fetch is required. Transient SEC failures therefore fail the request
      rather than silently serving stale data.
    * **Historical archived submissions** under ``SUBMISSIONS_ARCHIVE_BASE``
      (excluding the live ``CIK{cik}.json`` submissions document): treated as
      immutable once cached. They do not expire by age, but ``force_refresh``
      / ``ingest --refresh`` still bypasses and replaces them on success.
    * Cache freshness is independent of point-in-time financial
      ``available_at`` timestamps. A fresh HTTP payload can still contain
      facts whose PIT availability is in the past; conversely, a cached
      payload within TTL may lag the live SEC by up to the TTL window.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        force_refresh: bool = False,
    ) -> None:
        self._settings = settings
        self._force_refresh = force_refresh
        self._rate_limiter = _RateLimiter(settings.max_requests_per_second)
        self._owns_client = client is None
        self._cache_stats = CacheAccessStats()
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

    @property
    def cache_stats(self) -> CacheAccessStats:
        """Return observed cache-hit / network-fetch counts for this instance."""
        return self._cache_stats

    @property
    def force_refresh(self) -> bool:
        """Whether this client bypasses valid cache entries."""
        return self._force_refresh

    def get_json(
        self,
        url: str,
        *,
        use_cache: bool | None = None,
        force_refresh: bool | None = None,
    ) -> Any:
        """GET a JSON payload with rate limiting, retries, and optional caching.

        Expired mutable-endpoint cache entries are never reused. When the SEC
        is unavailable after retries, the error propagates — callers must not
        treat a failed refresh as a successful ingest of stale data.
        """
        cache_enabled = self._settings.enable_cache if use_cache is None else use_cache
        refresh = self._force_refresh if force_refresh is None else force_refresh

        if cache_enabled and not refresh:
            cached = self._read_cache(url)
            if cached is not None:
                self._cache_stats.cache_hits += 1
                self._cache_stats.urls_from_cache.append(url)
                logger.debug("Cache hit for %s", url)
                return cached
        elif cache_enabled and refresh:
            self._cache_stats.forced_refreshes += 1

        payload = self._get_json_with_retry(url)
        self._cache_stats.network_fetches += 1
        self._cache_stats.urls_fetched.append(url)
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
        safe_name = sanitize_archive_filename(filename)
        url = f"{SUBMISSIONS_ARCHIVE_BASE}{safe_name}"
        data = self.get_json(url)
        if not isinstance(data, dict):
            raise SecClientError(f"Unexpected archived submissions payload: {safe_name}")
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

    def _is_immutable_cache_url(self, url: str) -> bool:
        """Historical archive files are immutable; live CIK submissions are not."""
        if not url.startswith(SUBMISSIONS_ARCHIVE_BASE):
            return False
        name = url[len(SUBMISSIONS_ARCHIVE_BASE) :]
        # Live submissions: CIK0000320193.json — mutable, TTL applies.
        if re.fullmatch(r"CIK\d+\.json", name, flags=re.IGNORECASE):
            return False
        return bool(_ARCHIVE_FILENAME_RE.fullmatch(name))

    def _cache_is_fresh(self, url: str, path: Path) -> bool:
        if self._is_immutable_cache_url(url):
            return True
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age <= self._settings.sec_cache_ttl_seconds

    def _read_cache(self, url: str) -> Any | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        if not self._cache_is_fresh(url, path):
            self._cache_stats.expired_skips += 1
            logger.debug(
                "Cache expired for %s (TTL=%ss)",
                url,
                self._settings.sec_cache_ttl_seconds,
            )
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring corrupt cache file %s", path)
            return None

    def _write_cache(self, url: str, payload: Any) -> None:
        path = self._cache_path(url)
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("Failed to write cache file %s", path.name)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def sanitize_archive_filename(filename: str) -> str:
    """
    Accept only a basename-style SEC archive filename.

    Rejects path separators, ``..`` traversal, and unexpected characters so a
    malformed submissions payload cannot redirect the client outside
    ``/submissions/``.
    """
    text = str(filename or "").strip()
    if not text:
        raise SecClientError("Archived submissions filename must not be empty.")
    if text != Path(text).name or "/" in text or "\\" in text or ".." in text:
        raise SecClientError(f"Refusing unsafe archived submissions filename: {filename!r}")
    if not _ARCHIVE_FILENAME_RE.fullmatch(text):
        raise SecClientError(f"Refusing unexpected archived submissions filename: {filename!r}")
    return text
