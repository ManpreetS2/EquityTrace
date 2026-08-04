"""Twelve Data HTTP market-data provider (time_series / 1day)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from equitytrace.config import Settings
from equitytrace.market.models import (
    DailyBarsResponse,
    MarketDataProviderName,
    PriceAdjustmentMode,
    ProviderBar,
)
from equitytrace.market.providers.errors import (
    MarketDataAuthError,
    MarketDataConfigError,
    MarketDataEmptyError,
    MarketDataError,
    MarketDataSymbolError,
    MarketDataTransientError,
    MarketDataValidationError,
)

logger = logging.getLogger(__name__)

TWELVE_DATA_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
# Calendar window size for multi-year requests. With start_date+end_date we omit
# outputsize (Twelve Data may truncate when outputsize is combined with a range).
_WINDOW_DAYS = 400
_WINDOW_OVERLAP_DAYS = 2
_MAX_RETRY_AFTER_SECONDS = 60.0
_SUPPORTED_ADJUST = {
    PriceAdjustmentMode.NONE: "none",
    PriceAdjustmentMode.ALL: "all",
}


def _redact(text: str, api_key: str) -> str:
    if not api_key:
        return text
    return text.replace(api_key, "[REDACTED]")


class MinuteRateLimiter:
    """Thread-safe minimum-interval limiter for requests-per-minute budgets.

    Uses a monotonic clock. Cache hits must not call ``wait``.
    Injectable sleep/monotonic hooks keep tests deterministic.
    """

    def __init__(
        self,
        max_requests_per_minute: float,
        *,
        sleep_fn: Callable[[float], None] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        if max_requests_per_minute <= 0:
            raise ValueError("max_requests_per_minute must be positive")
        self._min_interval = 60.0 / max_requests_per_minute
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._sleep = sleep_fn or time.sleep
        self._monotonic = monotonic_fn or time.monotonic
        self.wait_count = 0

    def wait(self) -> None:
        with self._lock:
            self.wait_count += 1
            now = self._monotonic()
            elapsed = now - self._last_request_at
            if self._last_request_at > 0 and elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
            self._last_request_at = self._monotonic()


def _wait_with_retry_after(retry_state: RetryCallState) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, MarketDataTransientError) and exc.retry_after is not None:
        return min(float(exc.retry_after), _MAX_RETRY_AFTER_SECONDS)
    return wait_exponential(multiplier=0.5, min=0.5, max=30)(retry_state)


class TwelveDataProvider:
    """Twelve Data ``/time_series`` client with windowed retrieval."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        rate_limiter: MinuteRateLimiter | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        key = (api_key if api_key is not None else settings.twelve_data_api_key).strip()
        if not key:
            raise MarketDataConfigError(
                "EQUITYTRACE_TWELVE_DATA_API_KEY is required for live market-data requests. "
                "Copy .env.example to .env and set your Twelve Data API key."
            )
        self._api_key = key
        self._rate_limiter = rate_limiter or MinuteRateLimiter(
            settings.market_data_requests_per_minute,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.market_data_timeout_seconds,
            transport=transport,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
        if settings.enable_cache:
            settings.market_data_cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return MarketDataProviderName.TWELVE_DATA.value

    @property
    def rate_limiter(self) -> MinuteRateLimiter:
        return self._rate_limiter

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TwelveDataProvider:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def fetch_daily_bars(
        self,
        provider_symbol: str,
        start_date: date,
        end_date: date,
        adjustment_mode: PriceAdjustmentMode,
    ) -> DailyBarsResponse:
        symbol = provider_symbol.strip()
        if not symbol:
            raise MarketDataSymbolError("Provider symbol must not be empty.")
        if start_date > end_date:
            raise MarketDataValidationError(
                f"start_date {start_date} is after end_date {end_date}."
            )
        if adjustment_mode not in _SUPPORTED_ADJUST:
            raise MarketDataValidationError(
                f"Twelve Data v0.3a supports adjustment modes "
                f"{sorted(m.value for m in _SUPPORTED_ADJUST)}, not {adjustment_mode.value}."
            )

        windows = date_windows(start_date, end_date, _WINDOW_DAYS)
        merged: dict[date, ProviderBar] = {}
        meta: dict[str, Any] = {"windows_requested": len(windows)}
        currency: str | None = None
        exchange_timezone: str | None = None
        malformed_total = 0
        duplicate_total = 0
        duplicate_row_total = 0
        conflicting_dates: set[date] = set()
        window_populated: list[bool] = []
        incomplete_interior: list[dict[str, str]] = []

        for window_start, window_end in windows:
            response = self._fetch_window(
                symbol=symbol,
                start_date=window_start,
                end_date=window_end,
                adjustment_mode=adjustment_mode,
            )
            populated = bool(response.bars)
            window_populated.append(populated)
            meta.update({k: v for k, v in response.meta.items() if k != "windows_requested"})
            malformed_total += response.malformed_row_count
            duplicate_total += response.duplicate_date_count
            duplicate_row_total += response.duplicate_row_count
            conflicting_dates.update(response.conflicting_duplicate_dates)

            if populated:
                if currency is None:
                    currency = response.currency
                    exchange_timezone = response.exchange_timezone
                else:
                    if response.currency != currency:
                        raise MarketDataValidationError(
                            f"Twelve Data currency changed across windows for {symbol}: "
                            f"{currency} → {response.currency}."
                        )
                    if response.exchange_timezone != exchange_timezone:
                        raise MarketDataValidationError(
                            f"Twelve Data exchange timezone changed across windows for "
                            f"{symbol}: {exchange_timezone} → {response.exchange_timezone}."
                        )

                earliest = response.bars[0].trading_date
                latest = response.bars[-1].trading_date
                if earliest > window_start:
                    meta.setdefault("windows_with_leading_gap", []).append(
                        {
                            "window_start": window_start.isoformat(),
                            "earliest_returned": earliest.isoformat(),
                            "window_end": window_end.isoformat(),
                            "latest_returned": latest.isoformat(),
                        }
                    )

            for bar in response.bars:
                if not (start_date <= bar.trading_date <= end_date):
                    continue
                if bar.trading_date in conflicting_dates:
                    # Already rejected across windows; count additional sightings.
                    duplicate_row_total += 1
                    duplicate_total += 1
                    continue
                existing = merged.get(bar.trading_date)
                if existing is None:
                    merged[bar.trading_date] = bar
                    continue
                duplicate_total += 1
                if _provider_bars_identical(existing, bar):
                    # Identical cross-window overlap: keep one, count the extra.
                    duplicate_row_total += 1
                    continue
                # Conflicting cross-window duplicate: reject the trading date entirely.
                del merged[bar.trading_date]
                conflicting_dates.add(bar.trading_date)
                duplicate_row_total += 2  # previously kept row + new conflicting row

        # Empty interior windows between populated windows are incomplete.
        if any(window_populated):
            first_pop = next(i for i, p in enumerate(window_populated) if p)
            last_pop = (
                len(window_populated)
                - 1
                - next(i for i, p in enumerate(reversed(window_populated)) if p)
            )
            for idx in range(first_pop + 1, last_pop):
                if not window_populated[idx]:
                    w_start, w_end = windows[idx]
                    incomplete_interior.append(
                        {
                            "window_start": w_start.isoformat(),
                            "window_end": w_end.isoformat(),
                        }
                    )

        bars = tuple(merged[d] for d in sorted(merged))
        if not bars:
            raise MarketDataEmptyError(
                f"No daily bars returned for {symbol} between {start_date} and {end_date}."
            )
        if currency is None or exchange_timezone is None:
            raise MarketDataValidationError(
                f"Twelve Data returned bars for {symbol} without trustworthy currency/timezone."
            )

        incomplete = bool(incomplete_interior)
        if incomplete_interior:
            meta["incomplete_interior_windows"] = incomplete_interior
        meta["malformed_row_count"] = malformed_total
        meta["duplicate_date_count"] = duplicate_total
        meta["duplicate_row_count"] = duplicate_row_total
        return DailyBarsResponse(
            provider=MarketDataProviderName.TWELVE_DATA,
            provider_symbol=symbol,
            adjustment_mode=adjustment_mode,
            currency=currency,
            exchange_timezone=exchange_timezone,
            bars=bars,
            meta=meta,
            malformed_row_count=malformed_total,
            duplicate_date_count=duplicate_total,
            duplicate_row_count=duplicate_row_total,
            conflicting_duplicate_dates=tuple(sorted(conflicting_dates)),
            incomplete=incomplete,
        )

    def _fetch_window(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        adjustment_mode: PriceAdjustmentMode,
    ) -> DailyBarsResponse:
        # Prefer omitting outputsize when both start_date and end_date are set.
        # Twelve Data may silently truncate when outputsize is combined with a range.
        params = {
            "symbol": symbol,
            "interval": "1day",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "adjust": _SUPPORTED_ADJUST[adjustment_mode],
            "order": "ASC",
            "apikey": self._api_key,
        }
        cache_key_params = {k: v for k, v in params.items() if k != "apikey"}
        if self._settings.enable_cache:
            cached = self._read_cache(cache_key_params)
            if cached is not None:
                return self._parse_payload(
                    cached,
                    symbol=symbol,
                    adjustment_mode=adjustment_mode,
                )

        payload = self._request_json(params)
        if self._settings.enable_cache and _is_cacheable_success(payload):
            self._write_cache(cache_key_params, payload)
        return self._parse_payload(
            payload,
            symbol=symbol,
            adjustment_mode=adjustment_mode,
        )

    def _request_json(self, params: dict[str, str]) -> dict[str, Any]:
        attempts = max(1, self._settings.market_data_max_retries)

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=_wait_with_retry_after,
            retry=retry_if_exception_type(MarketDataTransientError),
        )
        def _once() -> dict[str, Any]:
            self._rate_limiter.wait()
            safe_qs = urlencode({k: v for k, v in params.items() if k != "apikey"})
            logger.debug("GET %s?%s", TWELVE_DATA_TIME_SERIES_URL, safe_qs)
            try:
                response = self._client.get(TWELVE_DATA_TIME_SERIES_URL, params=params)
            except httpx.TimeoutException as exc:
                raise MarketDataTransientError("Twelve Data request timed out.") from exc
            except httpx.TransportError as exc:
                raise MarketDataTransientError(
                    f"Twelve Data transport error: {_redact(str(exc), self._api_key)}"
                ) from exc

            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if response.status_code in {401, 403}:
                raise MarketDataAuthError("Twelve Data rejected the API key or denied access.")
            if response.status_code == 429 or response.status_code >= 500:
                raise MarketDataTransientError(
                    f"Twelve Data HTTP {response.status_code}.",
                    status_code=response.status_code,
                    retry_after=retry_after,
                )
            if response.status_code == 400:
                raise MarketDataValidationError("Twelve Data rejected the request (HTTP 400).")
            if response.status_code >= 400:
                raise MarketDataError(f"Twelve Data HTTP {response.status_code}.")
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise MarketDataValidationError("Twelve Data returned invalid JSON.") from exc
            if not isinstance(payload, dict):
                raise MarketDataValidationError("Twelve Data returned an unexpected JSON shape.")
            # Provider may return HTTP 200 with a structured error object.
            _raise_if_transient_payload_error(payload, api_key=self._api_key)
            return payload

        return _once()

    def _parse_payload(
        self,
        payload: dict[str, Any],
        *,
        symbol: str,
        adjustment_mode: PriceAdjustmentMode,
    ) -> DailyBarsResponse:
        status = str(payload.get("status") or "").lower()
        code = payload.get("code")
        message = str(payload.get("message") or payload.get("status") or "")
        redacted_message = _redact(message, self._api_key)

        if status == "error" or (isinstance(code, int) and code >= 400):
            lowered = redacted_message.lower()
            if "api key" in lowered or code in {401, 403}:
                raise MarketDataAuthError("Twelve Data rejected the API key or denied access.")
            if code == 429:
                raise MarketDataTransientError(
                    "Twelve Data rate limit exceeded.",
                    status_code=429,
                )
            if isinstance(code, int) and code >= 500:
                raise MarketDataTransientError(
                    f"Twelve Data upstream error for {symbol}: {redacted_message or code}",
                    status_code=code,
                )
            if "symbol" in lowered or code in {400, 404}:
                raise MarketDataSymbolError(f"Twelve Data does not recognize symbol '{symbol}'.")
            raise MarketDataError(
                f"Twelve Data error for {symbol}: {redacted_message or 'unknown error'}"
            )

        values = payload.get("values")
        if values is None:
            raise MarketDataEmptyError(f"No daily bars returned for {symbol}.")
        if not isinstance(values, list):
            raise MarketDataValidationError("Twelve Data 'values' field must be a list.")

        meta_obj = payload.get("meta")
        meta: dict[str, Any] = meta_obj if isinstance(meta_obj, dict) else {}

        # Empty windows are allowed (pre-listing / holidays / interior incompleteness).
        # Metadata is required only when bars are present so we never stamp defaults.
        if not values:
            return DailyBarsResponse(
                provider=MarketDataProviderName.TWELVE_DATA,
                provider_symbol=symbol,
                adjustment_mode=adjustment_mode,
                currency="",
                exchange_timezone="",
                bars=(),
                meta={k: v for k, v in meta.items() if "key" not in str(k).lower()},
                malformed_row_count=0,
                duplicate_date_count=0,
                duplicate_row_count=0,
                conflicting_duplicate_dates=(),
                incomplete=False,
            )

        currency, exchange_timezone = require_provider_meta(meta)

        by_date: dict[date, ProviderBar] = {}
        malformed = 0
        duplicate_dates = 0
        duplicate_rows = 0
        conflicting: set[date] = set()
        for raw in values:
            if not isinstance(raw, dict):
                malformed += 1
                continue
            try:
                bar = parse_bar(
                    raw,
                    currency=currency,
                    exchange_timezone=exchange_timezone,
                )
            except MarketDataValidationError:
                malformed += 1
                continue
            if bar.trading_date in conflicting:
                duplicate_rows += 1
                duplicate_dates += 1
                continue
            existing = by_date.get(bar.trading_date)
            if existing is None:
                by_date[bar.trading_date] = bar
                continue
            duplicate_dates += 1
            duplicate_rows += 1
            if _provider_bars_identical(existing, bar):
                # Identical duplicate: keep the first, reject the extra.
                continue
            # Conflicting duplicate: reject the trading date entirely.
            del by_date[bar.trading_date]
            conflicting.add(bar.trading_date)
            duplicate_rows += 1  # also reject the previously accepted row

        bars = tuple(by_date[d] for d in sorted(by_date))
        return DailyBarsResponse(
            provider=MarketDataProviderName.TWELVE_DATA,
            provider_symbol=symbol,
            adjustment_mode=adjustment_mode,
            currency=currency,
            exchange_timezone=exchange_timezone,
            bars=bars,
            meta={k: v for k, v in meta.items() if "key" not in str(k).lower()},
            malformed_row_count=malformed,
            duplicate_date_count=duplicate_dates,
            duplicate_row_count=duplicate_rows,
            conflicting_duplicate_dates=tuple(sorted(conflicting)),
            incomplete=False,
        )

    def _cache_path(self, params: dict[str, str]) -> Path:
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode("utf-8")).hexdigest()
        return self._settings.market_data_cache_dir / f"td_{digest}.json"

    def _read_cache(self, params: dict[str, str]) -> dict[str, Any] | None:
        path = self._cache_path(params)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring corrupt market cache file %s", path.name)
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        if not isinstance(payload, dict) or not _is_cacheable_success(payload):
            logger.warning("Ignoring non-success market cache file %s", path.name)
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        return payload

    def _write_cache(self, params: dict[str, str], payload: dict[str, Any]) -> None:
        path = self._cache_path(params)
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("Failed to write market cache file %s", path.name)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def date_windows(start: date, end: date, window_days: int) -> list[tuple[date, date]]:
    """Split an inclusive date range into overlapping calendar windows."""
    if start > end:
        return []
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    windows: list[tuple[date, date]] = []
    cursor = start
    overlap = timedelta(days=_WINDOW_OVERLAP_DAYS)
    while cursor <= end:
        window_end = min(cursor + timedelta(days=window_days - 1), end)
        windows.append((cursor, window_end))
        if window_end >= end:
            break
        next_cursor = window_end - overlap + timedelta(days=1)
        if next_cursor <= cursor:
            next_cursor = window_end + timedelta(days=1)
        cursor = next_cursor
    return windows


# Backwards-compatible private alias used by older tests.
_date_windows = date_windows


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as integer seconds or HTTP-date; cap and ignore invalid."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
        if seconds < 0:
            return None
        return min(seconds, _MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delay = (when.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if delay < 0:
        return 0.0
    return min(delay, _MAX_RETRY_AFTER_SECONDS)


def _raise_if_transient_payload_error(payload: dict[str, Any], *, api_key: str) -> None:
    """Raise retryable errors for HTTP-200 provider error objects before caching."""
    status = str(payload.get("status") or "").lower()
    code = payload.get("code")
    message = _redact(str(payload.get("message") or ""), api_key)
    if not (status == "error" or (isinstance(code, int) and code >= 400)):
        return
    if code == 429:
        raise MarketDataTransientError(
            "Twelve Data rate limit exceeded.",
            status_code=429,
        )
    if isinstance(code, int) and code >= 500:
        raise MarketDataTransientError(
            f"Twelve Data upstream error: {message or code}",
            status_code=code,
        )


def _is_cacheable_success(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").lower()
    if status == "error":
        return False
    code = payload.get("code")
    if isinstance(code, int) and code >= 400:
        return False
    values = payload.get("values")
    return isinstance(values, list)


def require_provider_meta(meta: dict[str, Any]) -> tuple[str, str]:
    """Require trustworthy currency and exchange timezone from provider meta."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if "currency" not in meta or meta.get("currency") is None:
        raise MarketDataValidationError("Twelve Data response missing currency metadata.")
    currency = str(meta.get("currency")).strip()
    if not currency:
        raise MarketDataValidationError("Twelve Data response has blank currency metadata.")

    tz_raw = meta.get("exchange_timezone")
    if tz_raw is None or (isinstance(tz_raw, str) and not str(tz_raw).strip()):
        tz_raw = meta.get("timezone")
    if tz_raw is None:
        raise MarketDataValidationError(
            "Twelve Data response missing exchange_timezone/timezone metadata."
        )
    exchange_timezone = str(tz_raw).strip()
    if not exchange_timezone:
        raise MarketDataValidationError(
            "Twelve Data response has blank exchange timezone metadata."
        )
    try:
        ZoneInfo(exchange_timezone)
    except ZoneInfoNotFoundError as exc:
        raise MarketDataValidationError(
            f"Twelve Data returned unknown exchange timezone '{exchange_timezone}'."
        ) from exc
    return currency, exchange_timezone


def _provider_bars_identical(left: ProviderBar, right: ProviderBar) -> bool:
    return (
        left.open == right.open
        and left.high == right.high
        and left.low == right.low
        and left.close == right.close
        and left.volume == right.volume
        and left.currency == right.currency
        and left.exchange_timezone == right.exchange_timezone
    )


def parse_bar(
    raw: dict[str, Any],
    *,
    currency: str,
    exchange_timezone: str,
) -> ProviderBar:
    datetime_raw = raw.get("datetime") or raw.get("date")
    if datetime_raw is None or (isinstance(datetime_raw, str) and not datetime_raw.strip()):
        raise MarketDataValidationError("Missing datetime")
    try:
        trading_date = date.fromisoformat(str(datetime_raw).strip()[:10])
    except ValueError as exc:
        raise MarketDataValidationError("Invalid datetime") from exc

    try:
        open_ = _parse_price(raw.get("open"), "open")
        high = _parse_price(raw.get("high"), "high")
        low = _parse_price(raw.get("low"), "low")
        close = _parse_price(raw.get("close"), "close")
        volume = _parse_volume(raw.get("volume", "0"))
    except MarketDataValidationError:
        raise
    except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
        raise MarketDataValidationError("Malformed numeric OHLC/volume") from exc

    return ProviderBar(
        trading_date=trading_date,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        currency=currency,
        exchange_timezone=exchange_timezone,
        metadata={"provider_datetime": str(datetime_raw)},
    )


def _parse_price(value: object, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise MarketDataValidationError(f"Invalid {field}")
    if isinstance(value, str) and not value.strip():
        raise MarketDataValidationError(f"Blank {field}")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataValidationError(f"Malformed {field}") from exc
    if not number.is_finite():
        raise MarketDataValidationError(f"Non-finite {field}")
    return number


def _parse_volume(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise MarketDataValidationError("Invalid volume")
    if isinstance(value, str) and not value.strip():
        raise MarketDataValidationError("Blank volume")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataValidationError("Malformed volume") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise MarketDataValidationError("Non-integer volume")
    volume = int(number)
    if volume < 0:
        raise MarketDataValidationError("Negative volume")
    return volume


# Backwards-compatible name used by service/tests.
_parse_bar = parse_bar


def validate_ohlcv(
    *,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
) -> list[str]:
    """Return validation failure reasons for an OHLCV row."""
    reasons: list[str] = []
    for label, value in (
        ("open", open_),
        ("high", high),
        ("low", low),
        ("close", close),
    ):
        if not value.is_finite():
            reasons.append(f"non_finite_{label}")
        elif value <= 0:
            reasons.append("non_positive_price")
    if volume < 0:
        reasons.append("negative_volume")
    if high < open_ or high < close:
        reasons.append("high_below_open_or_close")
    if low > open_ or low > close:
        reasons.append("low_above_open_or_close")
    if high < low:
        reasons.append("high_below_low")
    return reasons
