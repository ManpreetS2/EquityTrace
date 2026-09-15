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
    """Return decision sessions using the stored reference calendar only.

    Monthly: last stored in-window session of each represented month.
    Quarterly: last stored in-window session in March/June/September/December.
    Explicit: first-seen unique dates, chronological, fail-closed (see
    ``resolve_rebalance_pairs``).
    """
    if not sessions:
        raise CalendarError("calendar_missing")
    if schedule is PortfolioSchedule.EXPLICIT:
        pairs = _explicit_rebalance_pairs(
            sessions,
            explicit_dates,
            start_date=start_date,
            end_date=end_date,
        )
        return [decision for decision, _effective in pairs]
    in_window = [s for s in sessions if start_date <= s.trading_date <= end_date]
    if schedule is PortfolioSchedule.MONTHLY:
        return _last_session_per_month(in_window)
    return _last_session_in_quarter_months(in_window)


def resolve_rebalance_pairs(
    sessions: Sequence[CalendarSession],
    *,
    schedule: PortfolioSchedule,
    start_date: date,
    end_date: date,
    explicit_dates: Sequence[date] = (),
) -> list[tuple[CalendarSession, CalendarSession]]:
    """Return ``(decision, target_effective)`` pairs.

    Explicit schedules fail closed: every unique requested date must form a
    valid in-window pair. Monthly/quarterly decisions whose effective session
    is missing or after ``end_date`` are not executable and are omitted.
    """
    if schedule is PortfolioSchedule.EXPLICIT:
        return _explicit_rebalance_pairs(
            sessions,
            explicit_dates,
            start_date=start_date,
            end_date=end_date,
        )
    decisions = resolve_decision_sessions(
        sessions,
        schedule=schedule,
        start_date=start_date,
        end_date=end_date,
    )
    pairs: list[tuple[CalendarSession, CalendarSession]] = []
    for decision in decisions:
        effective = next_session(sessions, decision)
        if effective is None or effective.trading_date > end_date:
            continue
        pairs.append((decision, effective))
    if not pairs:
        raise CalendarError("calendar_missing")
    return pairs


def _explicit_rebalance_pairs(
    sessions: Sequence[CalendarSession],
    explicit_dates: Sequence[date],
    *,
    start_date: date,
    end_date: date,
) -> list[tuple[CalendarSession, CalendarSession]]:
    by_date = {session.trading_date: session for session in sessions}
    unique: list[date] = []
    seen: set[date] = set()
    for raw in explicit_dates:
        if raw in seen:
            continue
        seen.add(raw)
        unique.append(raw)
    pairs: list[tuple[CalendarSession, CalendarSession]] = []
    for raw in unique:
        if not (start_date <= raw <= end_date):
            raise CalendarError(
                "explicit_date_out_of_range",
                f"Explicit date {raw.isoformat()} is outside "
                f"[{start_date.isoformat()}, {end_date.isoformat()}]",
            )
        decision = by_date.get(raw)
        if decision is None:
            raise CalendarError(
                "calendar_missing",
                f"Explicit date {raw.isoformat()} is not a stored calendar session",
            )
        effective = next_session(sessions, decision)
        if effective is None:
            raise CalendarError(
                "calendar_missing",
                f"Explicit date {raw.isoformat()} has no later calendar session",
            )
        if effective.trading_date > end_date:
            raise CalendarError(
                "effective_session_out_of_range",
                f"Target-effective session {effective.trading_date.isoformat()} "
                f"is after {end_date.isoformat()}",
            )
        pairs.append((decision, effective))
    if not pairs:
        raise CalendarError("calendar_missing")
    pairs.sort(key=lambda item: item[0].trading_date)
    return pairs


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
