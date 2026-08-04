"""Point-in-time shares-outstanding selection from stored SEC facts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import duckdb

from equitytrace.market.models import SharesOutstandingResult
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.securities import SecuritiesRepository

# Priority: (taxonomy, concept). Lower index = higher priority.
_SHARES_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
)

_STALE_DAYS = 120


class SharesOutstandingService:
    """Select historically safe shares outstanding from SEC Company Facts."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._securities = SecuritiesRepository(conn)
        self._market = MarketRepository(conn)

    def get_shares_outstanding(
        self,
        ticker: str,
        *,
        market_date: date,
        knowledge_time: datetime,
    ) -> SharesOutstandingResult:
        symbol = ticker.strip().upper()
        knowledge = (
            knowledge_time if knowledge_time.tzinfo else knowledge_time.replace(tzinfo=UTC)
        ).astimezone(UTC)

        cik = self._securities.resolve_cik(symbol)
        warnings: list[str] = []
        if cik is None:
            return SharesOutstandingResult(
                ticker=symbol,
                shares=None,
                unavailable_reason="no_sec_security_for_ticker",
                warnings=tuple(warnings),
            )

        security_count = self._market.count_securities_for_issuer(cik)
        if security_count > 1:
            warnings.append(
                "multiple_active_share_classes:"
                f"issuer {cik} has {security_count} listed securities; "
                "share count may be issuer-wide rather than class-specific"
            )

        candidates: list[dict[str, object]] = []
        for priority, (taxonomy, concept) in enumerate(_SHARES_CANDIDATES):
            rows = self._conn.execute(
                """
                SELECT f.value, f.end_date, f.available_at, f.accession_number, f.form,
                       f.concept, f.taxonomy, f.unit, f.filing_date, f.acceptance_datetime
                FROM financial_facts f
                WHERE f.cik = ?
                  AND lower(f.taxonomy) = lower(?)
                  AND f.concept = ?
                  AND lower(f.unit) = 'shares'
                  AND f.value > 0
                  AND f.end_date IS NOT NULL
                  AND f.end_date <= ?
                  AND f.available_at <= ?
                ORDER BY f.end_date DESC, f.available_at DESC, f.accession_number DESC
                """,
                [cik, taxonomy, concept, market_date, knowledge],
            ).fetchall()
            for row in rows:
                candidates.append(
                    {
                        "priority": priority,
                        "value": Decimal(str(row[0])),
                        "fact_date": row[1],
                        "available_at": row[2],
                        "accession_number": row[3],
                        "form": row[4],
                        "concept": row[5],
                        "taxonomy": row[6],
                        "unit": row[7],
                    }
                )

        if not candidates:
            return SharesOutstandingResult(
                ticker=symbol,
                shares=None,
                unavailable_reason="no_qualifying_shares_outstanding_fact",
                warnings=tuple(warnings),
            )

        # Prefer latest fact_date, then higher-priority concept, then latest available_at.
        def _sort_key(c: dict[str, object]) -> tuple[object, int, object, str]:
            priority = c["priority"]
            assert isinstance(priority, int)
            return (
                c["fact_date"],
                -priority,
                c["available_at"],
                str(c["accession_number"] or ""),
            )

        candidates.sort(key=_sort_key, reverse=True)
        best = candidates[0]
        equals = [
            c
            for c in candidates
            if c["fact_date"] == best["fact_date"]
            and c["priority"] == best["priority"]
            and c["value"] != best["value"]
        ]
        if equals:
            warnings.append("conflicting_equal_priority_shares_facts")

        if best["accession_number"] is None:
            warnings.append("missing_accession_provenance")

        fact_date = best["fact_date"]
        assert isinstance(fact_date, date)
        age = market_date - fact_date
        if age > timedelta(days=_STALE_DAYS):
            warnings.append(f"stale_shares_fact:{age.days}_days_old")

        if security_count > 1:
            warnings.append("potential_issuer_level_share_scope")

        shares_val = best["value"]
        available_at = best["available_at"]
        if not isinstance(shares_val, Decimal):
            shares_val = Decimal(str(shares_val))
        if available_at is not None and not isinstance(available_at, datetime):
            raise TypeError("available_at must be datetime")
        return SharesOutstandingResult(
            ticker=symbol,
            shares=shares_val,
            fact_date=fact_date,
            available_at=available_at,
            accession_number=str(best["accession_number"])
            if best["accession_number"] is not None
            else None,
            form=str(best["form"]) if best["form"] is not None else None,
            concept=str(best["concept"]),
            taxonomy=str(best["taxonomy"]),
            unit=str(best["unit"]),
            warnings=tuple(dict.fromkeys(warnings)),
        )
