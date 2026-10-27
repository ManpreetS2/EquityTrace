"""Fail-closed admission checks for complete SEC issuer replacements."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date
from typing import Any

from equitytrace.models import Filing, FinancialFact, Issuer, Security, normalize_cik

MAX_FACTS = 250_000
MAX_FILINGS = 50_000
MAX_TEXT_BYTES = 128 * 1024 * 1024


def validate_payloads(submissions: dict[str, Any], company_facts: dict[str, Any], cik: str) -> None:
    """Reject malformed/incomplete shapes before normalizers can skip evidence.

    SEC duplicate observations may normalize to one natural key. Missing history
    relative to the stored snapshot is checked separately before any DELETE.
    """
    for payload in (submissions, company_facts):
        if normalize_cik(payload.get("cik", "")) != cik:
            raise ValueError("SEC payload CIK does not match the resolved issuer")
    recent = submissions.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    if not accessions or len(accessions) > MAX_FILINGS:
        raise ValueError("Missing, empty or oversized SEC filing history")
    for key in ("accessionNumber", "filingDate", "form"):
        values = recent.get(key)
        if not isinstance(values, list) or len(values) != len(accessions) or not all(values):
            raise ValueError(f"Incomplete SEC filing array: {key}")
    seen: dict[str, tuple[object, ...]] = {}
    for index, accession in enumerate(accessions):
        date.fromisoformat(recent["filingDate"][index])
        row = tuple(
            values[index] if isinstance(values, list) and index < len(values) else None
            for key, values in sorted(recent.items())
        )
        if accession in seen and seen[accession] != row:
            raise ValueError(f"Conflicting duplicate filing accession: {accession}")
        seen[accession] = row

    root = company_facts.get("facts")
    if not isinstance(root, dict) or not root:
        raise ValueError("Missing or empty Company Facts evidence")
    observations = 0
    for concepts in root.values():
        if not isinstance(concepts, dict):
            raise ValueError("Malformed Company Facts taxonomy")
        for concept in concepts.values():
            if not isinstance(concept, dict) or not isinstance(concept.get("units"), dict):
                raise ValueError("Malformed Company Facts concept/units")
            for values in concept["units"].values():
                if not isinstance(values, list):
                    raise ValueError("Malformed Company Facts observations")
                for value in values:
                    observations += 1
                    if observations > MAX_FACTS:
                        raise ValueError("SEC snapshot exceeds supported fact limit; not truncated")
                    if not isinstance(value, dict) or not value.get("accn"):
                        raise ValueError("Fact lacks accession provenance")
                    if isinstance(value.get("val"), bool) or not math.isfinite(float(value["val"])):
                        raise ValueError("Invalid/nonfinite financial fact")
                    for key in ("end", "filed"):
                        date.fromisoformat(value[key])
                    # A malformed reported duration must not silently become an
                    # instant fact when normalization cannot parse its start.
                    if value.get("start") is not None:
                        date.fromisoformat(value["start"])
    if not observations:
        raise ValueError("Empty Company Facts observations")
    # A bounded traversal adds no full-payload JSON copy. Fetching already parsed
    # JSON is existing provider behavior; this bounds admitted normalization work.
    text_size = 0
    for payload in (submissions, company_facts):
        for value in _strings(payload):
            text_size += len(value.encode("utf-8"))
            if text_size > MAX_TEXT_BYTES:
                raise ValueError("SEC snapshot text exceeds supported size; not truncated")


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def coverage_key(fact: FinancialFact) -> tuple[object, ...]:
    """Observation identity excluding a corrected numeric value and availability."""
    return (
        fact.taxonomy,
        fact.concept,
        fact.unit,
        fact.start_date,
        fact.end_date,
        fact.accession_number,
        fact.form,
        fact.fiscal_year,
        fact.fiscal_period,
        fact.frame,
    )


def validate_snapshot(
    issuer: Issuer,
    securities: list[Security],
    filings: list[Filing],
    facts: list[FinancialFact],
    *,
    require_complete: bool,
) -> set[tuple[object, ...]]:
    """Validate typed input without changing fact IDs or source provenance."""
    if require_complete and (not securities or not filings or not facts):
        raise ValueError("Ambiguous empty issuer replacement; previous snapshot retained")
    if len(facts) > MAX_FACTS or len(filings) > MAX_FILINGS:
        raise ValueError("SEC snapshot exceeds supported row limit; not truncated")
    if any(item.cik != issuer.cik for item in securities) or any(
        item.cik != issuer.cik for item in filings
    ):
        raise ValueError("Mixed-CIK issuer replacement")
    fact_ids: set[str] = set()
    coverage: set[tuple[object, ...]] = set()
    for fact in facts:
        if fact.cik != issuer.cik:
            raise ValueError("Mixed-CIK issuer replacement")
        if not fact.fact_id or fact.fact_id in fact_ids or not math.isfinite(fact.value):
            raise ValueError("Missing/duplicate fact ID or nonfinite fact")
        fact_ids.add(fact.fact_id)
        coverage.add(coverage_key(fact))
    for identifiers in ([s.ticker for s in securities], [f.accession_number for f in filings]):
        if any(not key for key in identifiers) or len(set(identifiers)) != len(identifiers):
            raise ValueError("Missing/duplicate security or filing identifier")
    return coverage
