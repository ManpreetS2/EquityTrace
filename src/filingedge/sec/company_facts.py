"""XBRL Company Facts retrieval helpers."""

from __future__ import annotations

from typing import Any

from filingedge.sec.client import SecClient


def fetch_company_facts(client: SecClient, cik: str) -> dict[str, Any]:
    """Fetch Company Facts JSON for a CIK."""
    return client.get_company_facts(cik)
