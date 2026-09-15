"""Reference-calendar sessions independent of changing portfolio members."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from equitytrace.market.models import DailyPriceBar
from equitytrace.portfolio.models import PortfolioSchedule


class CalendarError(RuntimeError):
    """Reference calendar cannot be resolved."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True, slots=True)
class CalendarSession:
    trading_date: date
    available_at: datetime
    instrument_id: str


def sessions_from_bars(bars: Sequence[DailyPriceBar]) -> list[CalendarSession]:
    """Build ordered unique sessions. Duplicate dates are not a calendar."""
    by_date: dict[date, DailyPriceBar] = {}
    for bar in bars:
        existing = by_date.get(bar.trading_date)
        if existing is not None:
            raise CalendarError("calendar_missing", "Duplicate calendar session dates")
        by_date[bar.trading_date] = bar
    sessions: list[CalendarSession] = []
    for day in sorted(by_date):
        bar = by_date[day]
        sessions.append(
            CalendarSession(
                trading_date=day,
                available_at=bar.available_at,
                instrument_id=bar.instrument_id,
            )
        )
    return sessions


def next_session(
    sessions: Sequence[CalendarSession],
    current: CalendarSession,
) -> CalendarSession | None:
    for session in sessions:
        if session.trading_date > current.trading_date:
            return session
    return None


def resolve_decision_sessions(
    sessions: Sequence[CalendarSession],
    *,
    schedule: PortfolioSchedule,
    start_date: date,
    end_date: date,
    explicit_dates: Sequence[date] = (),
) -> list[CalendarSession]:
    """Return decision sessions using the stored reference calendar only."""
    if not sessions:
        raise CalendarError("calendar_missing")
    if schedule is PortfolioSchedule.EXPLICIT:
        return _explicit_sessions(sessions, explicit_dates)
    in_window = [s for s in sessions if start_date <= s.trading_date <= end_date]
    if schedule is PortfolioSchedule.MONTHLY:
        return _last_session_per_month(in_window)
    return _last_session_in_quarter_months(in_window)


def _explicit_sessions(
    sessions: Sequence[CalendarSession],
    explicit_dates: Sequence[date],
) -> list[CalendarSession]:
    by_date = {session.trading_date: session for session in sessions}
    resolved: list[CalendarSession] = []
    seen: set[date] = set()
    for raw in explicit_dates:
        if raw in seen:
            continue
        seen.add(raw)
        match = by_date.get(raw)
        if match is None:
            raise CalendarError(
                "calendar_missing",
                f"Explicit date {raw.isoformat()} is not a stored calendar session",
            )
        resolved.append(match)
    resolved.sort(key=lambda session: session.trading_date)
    return resolved


def _last_session_per_month(sessions: Sequence[CalendarSession]) -> list[CalendarSession]:
    last: dict[tuple[int, int], CalendarSession] = {}
    for session in sessions:
        last[(session.trading_date.year, session.trading_date.month)] = session
    return [last[key] for key in sorted(last)]


def _last_session_in_quarter_months(
    sessions: Sequence[CalendarSession],
) -> list[CalendarSession]:
    last: dict[tuple[int, int], CalendarSession] = {}
    for session in sessions:
        if session.trading_date.month not in {3, 6, 9, 12}:
            continue
        last[(session.trading_date.year, session.trading_date.month)] = session
    return [last[key] for key in sorted(last)]
