"""Market-data availability timestamp conventions."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from equitytrace.market.providers.errors import MarketDataValidationError

# Conservative EquityTrace end-of-day availability convention:
# treat a daily bar as usable after 16:15 exchange-local time.
# This is not a claim about the exact official publication instant
# for every exchange or data vendor.
EOD_AVAILABILITY_LOCAL_TIME = time(16, 15)


def bar_available_at(
    trading_date: date,
    exchange_timezone: str,
) -> datetime:
    """
    Return the UTC availability timestamp for a daily bar.

    ``available_at = exchange-local 16:15 on trading_date``, converted to UTC.
    Daylight-saving transitions are handled by the named zone.

    Unknown timezones raise ``MarketDataValidationError`` rather than falling
    back to the local machine timezone.
    """
    tz_name = (exchange_timezone or "").strip()
    if not tz_name:
        raise MarketDataValidationError("Missing exchange timezone for availability.")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise MarketDataValidationError(f"Unknown exchange timezone '{tz_name}'.") from exc
    local_dt = datetime.combine(trading_date, EOD_AVAILABILITY_LOCAL_TIME, tzinfo=tz)
    return local_dt.astimezone(ZoneInfo("UTC"))
