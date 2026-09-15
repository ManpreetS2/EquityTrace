"""Point-in-time financial statement snapshot service."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from equitytrace.financials.derived import compute_derived_values, derive_standalone_quarter
from equitytrace.financials.mappings import (
    BALANCE_CONCEPTS,
    CASH_FLOW_CONCEPTS,
    INCOME_CONCEPTS,
    all_candidate_concepts,
)
from equitytrace.financials.models import CanonicalValue, FinancialPeriod, FinancialSnapshot
from equitytrace.financials.periods import parse_period
from equitytrace.financials.selector import (
    component_gross_profit,
    component_short_term_debt,
    component_total_debt,
    earliest_period_bounds,
    select_canonical_value,
)
from equitytrace.models import FinancialFact
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.securities import SecuritiesRepository


class FinancialsError(RuntimeError):
    """Raised for user-facing financial statement errors."""


class FinancialsService:
    """Build canonical, point-in-time financial snapshots from stored facts."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._facts = FactsRepository(conn)
        self._securities = SecuritiesRepository(conn)

    def get_snapshot(
        self,
        ticker: str,
        period: str | FinancialPeriod,
        as_of: datetime | None = None,
    ) -> FinancialSnapshot:
        """
        Return a point-in-time canonical financial snapshot.

        Facts with ``available_at > as_of`` are never used.
        """
        symbol = ticker.strip().upper()
        cik = self._securities.resolve_cik(symbol)
        if cik is None:
            raise FinancialsError(
                f"No stored data for ticker {symbol}. Run `equitytrace ingest` first."
            )

        parsed = period if isinstance(period, FinancialPeriod) else parse_period(period)
        as_of_utc = _normalize_as_of(as_of)

        facts = self._load_candidate_facts(symbol, as_of_utc)
        warnings: list[str] = []
        resolved: dict[str, CanonicalValue] = {}

        for concept in (*INCOME_CONCEPTS, *BALANCE_CONCEPTS, *CASH_FLOW_CONCEPTS):
            result = select_canonical_value(
                concept=concept,
                period=parsed,
                facts=facts,
                as_of=as_of_utc,
                prefer_standalone_quarter=True,
            )
            value = result.value
            if (
                value.value is None
                and parsed.kind == "quarterly"
                and concept in {*INCOME_CONCEPTS, *CASH_FLOW_CONCEPTS}
            ):
                derived_q = derive_standalone_quarter(
                    concept=concept,
                    period=parsed,
                    facts=facts,
                    as_of=as_of_utc,
                )
                if derived_q is not None:
                    # Use successful derivation, or keep the rejected result so
                    # mismatch warnings / reasons are visible on the snapshot.
                    value = derived_q
            resolved[concept] = value
            warnings.extend(value.warnings)

        # Component fallbacks when allowed and direct values are missing.
        if resolved.get("gross_profit") is None or resolved["gross_profit"].value is None:
            gp = component_gross_profit(
                revenue=resolved.get("revenue"),
                cost=resolved.get("cost_of_revenue"),
            )
            if gp is not None:
                resolved["gross_profit"] = gp
                warnings.extend(gp.warnings)

        short = resolved.get("short_term_debt")
        selected_short_concept = (
            short.provenance.source_concept
            if short is not None and short.value is not None and not short.provenance.is_derived
            else None
        )
        needs_component_short = (
            short is None or short.value is None or selected_short_concept != "DebtCurrent"
        )
        if needs_component_short:
            # DebtCurrent is a complete current-debt total. Otherwise sum
            # non-overlapping components so CP/borrowings + current LTD are not dropped.
            std = component_short_term_debt(
                facts=facts,
                period=parsed,
                as_of=as_of_utc,
            )
            if std is not None:
                resolved["short_term_debt"] = std
                warnings.extend(std.warnings)

        if resolved.get("total_debt") is None or resolved["total_debt"].value is None:
            td = component_total_debt(
                short_term=resolved.get("short_term_debt"),
                long_term=resolved.get("long_term_debt"),
            )
            if td is not None:
                resolved["total_debt"] = td
                warnings.extend(td.warnings)

        derived = compute_derived_values(resolved)
        for item in derived.values():
            warnings.extend(item.warnings)

        income = {name: resolved[name] for name in INCOME_CONCEPTS if name in resolved}
        balance = {name: resolved[name] for name in BALANCE_CONCEPTS if name in resolved}
        cash = {name: resolved[name] for name in CASH_FLOW_CONCEPTS if name in resolved}

        present_values = [
            v
            for v in (*income.values(), *balance.values(), *cash.values(), *derived.values())
            if v.value is not None
        ]
        period_start, period_end = earliest_period_bounds(present_values)
        accessions = tuple(
            sorted(
                {
                    v.provenance.accession_number
                    for v in present_values
                    if v.provenance.accession_number
                }
            )
        )
        missing = tuple(
            sorted(
                name
                for name, value in {**income, **balance, **cash, **derived}.items()
                if value.value is None
            )
        )

        # Deduplicate warnings while preserving order.
        ordered_warnings = tuple(dict.fromkeys(warnings))

        return FinancialSnapshot(
            ticker=symbol,
            cik=cik,
            period=parsed,
            as_of=as_of_utc,
            period_start=period_start,
            period_end=period_end,
            income_statement=income,
            balance_sheet=balance,
            cash_flow=cash,
            derived=derived,
            source_accessions=accessions,
            warnings=ordered_warnings,
            missing=missing,
        )

    def list_available_annual_periods(
        self,
        ticker: str,
        as_of: datetime,
    ) -> list[FinancialPeriod]:
        """Return FY periods known as of ``as_of``, newest fiscal year first.

        Only facts with ``available_at <= as_of`` and ``fiscal_period = FY``
        contribute. Quarters are never annualized into a year.
        """
        symbol = ticker.strip().upper()
        as_of_utc = _normalize_as_of(as_of)
        rows = self._conn.execute(
            """
            SELECT DISTINCT f.fiscal_year
            FROM financial_facts f
            INNER JOIN securities s ON s.cik = f.cik
            WHERE s.ticker = ?
              AND f.available_at <= ?
              AND f.fiscal_year IS NOT NULL
              AND upper(f.fiscal_period) = 'FY'
            ORDER BY f.fiscal_year DESC
            """,
            [symbol, as_of_utc],
        ).fetchall()
        return [
            FinancialPeriod(fiscal_year=int(row[0]), fiscal_period="FY", kind="annual")
            for row in rows
            if row[0] is not None
        ]

    def _load_candidate_facts(self, ticker: str, as_of: datetime) -> list[FinancialFact]:
        """Load PIT facts limited to mapped XBRL concepts."""
        facts = self._facts.get_facts_as_of(ticker, as_of)
        candidates = all_candidate_concepts()
        return [f for f in facts if f.concept in candidates]


def _normalize_as_of(as_of: datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(UTC)
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=UTC)
    return as_of.astimezone(UTC)
