"""Shared point-in-time cutoff parsing for every ``--as-of`` CLI option."""

from __future__ import annotations

from datetime import UTC, date, datetime

from equitytrace.sec.normalization import EASTERN, end_of_eastern_day_utc

AS_OF_HELP = (
    "Point-in-time cutoff: YYYY-MM-DD = end of that day, US Eastern; "
    "datetime without zone = US Eastern; explicit Z/offset honored"
)


def parse_as_of(value: str) -> datetime:
    """Resolve an ``--as-of`` string to a UTC instant (cutoff is ``available_at <= as_of``).

    Rules (identical for every command):

    * Date only (``2024-02-15``): end of that day, 23:59:59 U.S. Eastern.
    * Datetime with an explicit zone (``Z`` or an offset): that absolute instant.
    * Datetime without a zone: U.S. Eastern wall time, matching SEC acceptance timestamps.
    """
    text = value.strip()
    try:
        if "T" in text or " " in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=EASTERN)
            return parsed.astimezone(UTC)
        return end_of_eastern_day_utc(date.fromisoformat(text))
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD or an ISO datetime.") from exc
