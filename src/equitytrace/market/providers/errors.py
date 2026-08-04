"""Market-data provider errors."""

from __future__ import annotations


class MarketDataError(RuntimeError):
    """Base market-data error safe for CLI display."""


class MarketDataConfigError(MarketDataError):
    """Missing or invalid market-data configuration."""


class MarketDataAuthError(MarketDataError):
    """Invalid or missing provider credentials."""


class MarketDataSymbolError(MarketDataError):
    """Unknown or unsupported provider symbol."""


class MarketDataEmptyError(MarketDataError):
    """Provider returned no usable bars for the request."""


class MarketDataIncompleteError(MarketDataError):
    """Provider response could not be proven complete for the requested range."""


class MarketDataTransientError(MarketDataError):
    """Timeout, connection, rate-limit, or server error eligible for retry."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class MarketDataValidationError(MarketDataError):
    """Malformed provider payload that should not be retried."""
