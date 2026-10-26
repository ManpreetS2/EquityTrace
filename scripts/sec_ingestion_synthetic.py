"""Deterministic SEC-shaped evidence for offline qualification only."""

from __future__ import annotations

import hashlib
import json


def make_payloads(count: int, scale: float = 1.0) -> tuple[dict, dict]:
    cik = "0000999999"
    filing_count = (count + 199) // 200
    accessions = [f"{cik}-26-{i + 1:06d}" for i in range(filing_count)]
    submissions = {
        "cik": cik,
        "name": "Synthetic SEC Qualification Corporation",
        "entityType": "operating",
        "tickers": ["SYNTH"],
        "exchanges": ["Nasdaq"],
        "filings": {
            "recent": {
                "accessionNumber": accessions,
                "form": ["10-K"] * filing_count,
                "filingDate": ["2026-01-02"] * filing_count,
                "reportDate": ["2025-12-31"] * filing_count,
                "acceptanceDateTime": ["2026-01-02T21:00:00Z"] * filing_count,
                "primaryDocument": [
                    f"synthetic-annual-report-{i}.htm" for i in range(filing_count)
                ],
                "isXBRL": [1] * filing_count,
            }
        },
    }
    concepts: dict = {}
    for i in range(count):
        year = 2000 + i % 25
        concept = concepts.setdefault(
            f"SyntheticReportedConcept{i % 256:03d}",
            {
                "label": f"Synthetic financial statement concept {i % 256}",
                "description": "Synthetic qualification evidence with realistic descriptive text. "
                * 6,
                "units": {"USD": []},
            },
        )
        concept["units"]["USD"].append(
            {
                "val": (100_000.125 + i) * scale,
                "start": f"{year}-01-01",
                "end": f"{year}-12-31",
                "filed": "2026-01-02",
                "accn": accessions[i // 200],
                "form": "10-K",
                "fy": year,
                "fp": "FY",
                "frame": f"CY{year}",
            }
        )
    return submissions, {
        "cik": int(cik),
        "entityName": submissions["name"],
        "facts": {"us-gaap": concepts},
    }


def fact_digest(conn) -> tuple[int, str]:
    """Stream the full domain fact content; exclude only persistence timestamps."""
    cursor = conn.execute(
        """SELECT fact_id, cik, taxonomy, concept, label, description, unit, value,
                  start_date, end_date, filing_date, acceptance_datetime, available_at,
                  accession_number, form, fiscal_year, fiscal_period, frame, source_url
           FROM financial_facts ORDER BY fact_id"""
    )
    digest = hashlib.sha256()
    count = 0
    while rows := cursor.fetchmany(2000):
        for row in rows:
            digest.update((json.dumps(row, default=str, separators=(",", ":")) + "\n").encode())
            count += 1
    return count, digest.hexdigest()
