"""Synthetic qualification profiles preserve all observations and provenance."""

import importlib
from pathlib import Path

import pytest

from equitytrace.sec.normalization import normalize_company_facts, normalize_filings
from equitytrace.sec.validation import validate_payloads


@pytest.mark.parametrize("profile", ["baseline", "large_text", "diverse"])
def test_envelope_profile_retains_evidence(profile, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("qualify_sec_envelope")
    submissions, raw = module.fixture(100, profile)
    validate_payloads(submissions, raw, "0000999999")
    filings = normalize_filings(submissions)
    facts = normalize_company_facts(
        raw, filings_by_accession={f.accession_number: f for f in filings}
    )
    assert len(facts) == 100
    assert len({f.fact_id for f in facts}) == 100
    assert all(f.accession_number in {x.accession_number for x in filings} for f in facts)
    assert all(f.available_at and f.acceptance_datetime and f.source_url for f in facts)
    if profile == "large_text":
        assert all(len(f.description) == 4096 and len(f.label) == 512 for f in facts)
    if profile == "diverse":
        assert len({f.concept for f in facts}) == 100
        assert {f.unit for f in facts} == {"USD", "shares"}
