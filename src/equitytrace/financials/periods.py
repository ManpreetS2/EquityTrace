"""Financial period parsing and comparable-period helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from equitytrace.financials.models import FinancialPeriod

_FY_RE = re.compile(r"^FY[\s\-]?(\d{4})$", re.IGNORECASE)
_Q_RE = re.compile(r"^Q([1-4])[\s\-](\d{4})$", re.IGNORECASE)
_ALT_Q_RE = re.compile(r"^(\d{4})Q([1-4])$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PeriodBounds:
    """Approximate calendar bounds used for duration heuristics."""

    fiscal_year: int
    fiscal_period: str


def parse_period(text: str) -> FinancialPeriod:
    """
    Parse a human period token into a FinancialPeriod.

    Accepted forms:
    - FY2023, FY-2023, FY 2023
    - Q3-2023, Q3 2023, 2023Q3
    """
    raw = text.strip()
    if not raw:
        raise ValueError("Period string is empty.")

    match = _FY_RE.match(raw)
    if match:
        year = int(match.group(1))
        return FinancialPeriod(fiscal_year=year, fiscal_period="FY", kind="annual")

    match = _Q_RE.match(raw)
    if match:
        quarter = int(match.group(1))
        year = int(match.group(2))
        return FinancialPeriod(
            fiscal_year=year,
            fiscal_period=f"Q{quarter}",
            kind="quarterly",
        )

    match = _ALT_Q_RE.match(raw)
    if match:
        year = int(match.group(1))
        quarter = int(match.group(2))
        return FinancialPeriod(
            fiscal_year=year,
            fiscal_period=f"Q{quarter}",
            kind="quarterly",
        )

    raise ValueError(f"Unrecognized period '{text}'. Use FY2023 or Q3-2023 (also accepts 2023Q3).")


def prior_comparable_period(period: FinancialPeriod) -> FinancialPeriod:
    """Return the prior-year comparable period (YoY)."""
    return FinancialPeriod(
        fiscal_year=period.fiscal_year - 1,
        fiscal_period=period.fiscal_period,
        kind=period.kind,
    )


def prior_quarter_same_year(period: FinancialPeriod) -> FinancialPeriod | None:
    """Return the immediately preceding quarter within the same fiscal year."""
    if period.kind != "quarterly":
        return None
    quarter = int(period.fiscal_period[1])
    if quarter <= 1:
        return None
    return FinancialPeriod(
        fiscal_year=period.fiscal_year,
        fiscal_period=f"Q{quarter - 1}",
        kind="quarterly",
    )


def ytd_label(period: FinancialPeriod) -> str:
    """Label used when a fact represents cumulative year-to-date values."""
    return f"{period.fiscal_period}-YTD-{period.fiscal_year}"


def period_matches_fact(
    period: FinancialPeriod, fiscal_year: int | None, fiscal_period: str | None
) -> bool:
    """Exact fiscal-year / fiscal-period match."""
    if fiscal_year is None or fiscal_period is None:
        return False
    return (
        fiscal_year == period.fiscal_year and fiscal_period.upper() == period.fiscal_period.upper()
    )


def duration_days(start: object | None, end: object | None) -> int | None:
    """Return inclusive-ish duration in days when both dates exist."""
    from datetime import date

    if not isinstance(start, date) or not isinstance(end, date):
        return None
    return (end - start).days


def looks_like_ytd_duration(days: int | None, period: FinancialPeriod) -> bool:
    """
    Heuristic: quarterly standalone durations are ~70-110 days.

    Longer cumulative spans for Q2/Q3/Q4 are treated as year-to-date.
    """
    if days is None or period.kind != "quarterly":
        return False
    quarter = int(period.fiscal_period[1])
    if quarter == 1:
        return False
    # Standalone quarter ~90 days; YTD Q2 ~180, Q3 ~270.
    return days > 130
