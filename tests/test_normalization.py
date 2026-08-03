"""Tests for SEC payload normalization and timestamps."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from filingedge.models import Filing
from filingedge.sec.normalization import (
    deduplicate_facts,
    normalize_company_facts,
    normalize_filings,
    normalize_issuer,
    normalize_securities,
    parse_acceptance_datetime,
    resolve_available_at,
)
from filingedge.sec.submissions import list_archive_filenames, merge_submissions

EASTERN = ZoneInfo("America/New_York")


def test_parse_compact_eastern_winter_to_utc() -> None:
    # EST (UTC-5): 2024-02-15 16:30:15 Eastern -> 21:30:15 UTC
    parsed = parse_acceptance_datetime("20240215163015")
    assert parsed == datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)
    assert parsed.tzinfo == UTC


def test_parse_compact_eastern_summer_dst_to_utc() -> None:
    # EDT (UTC-4): 2024-07-15 16:30:15 Eastern -> 20:30:15 UTC
    parsed = parse_acceptance_datetime("20240715163015")
    assert parsed == datetime(2024, 7, 15, 20, 30, 15, tzinfo=UTC)


def test_parse_iso_z_is_utc_not_rebased_as_eastern() -> None:
    # Live SEC submissions use ISO-Z UTC. Must NOT shift by Eastern offset.
    parsed = parse_acceptance_datetime("2024-02-15T16:30:15.000Z")
    assert parsed == datetime(2024, 2, 15, 16, 30, 15, tzinfo=UTC)


def test_parse_acceptance_before_midnight_eastern() -> None:
    # 23:30 Eastern on Feb 14 (EST) -> 04:30 UTC on Feb 15
    parsed = parse_acceptance_datetime("20240214233000")
    assert parsed == datetime(2024, 2, 15, 4, 30, 0, tzinfo=UTC)


def test_parse_acceptance_datetime_empty() -> None:
    assert parse_acceptance_datetime(None) is None
    assert parse_acceptance_datetime("") is None


def test_conservative_filing_date_fallback() -> None:
    available = resolve_available_at(acceptance_datetime=None, filing_date=date(2020, 10, 30))
    expected = datetime(2020, 10, 30, 23, 59, 59, tzinfo=EASTERN).astimezone(UTC)
    assert available == expected
    start_of_day = datetime(2020, 10, 30, 0, 0, 0, tzinfo=EASTERN).astimezone(UTC)
    assert available > start_of_day


def test_naive_acceptance_treated_as_eastern() -> None:
    naive = datetime(2024, 2, 15, 16, 30, 15)
    available = resolve_available_at(acceptance_datetime=naive, filing_date=date(2024, 2, 15))
    assert available == datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)


def test_available_at_prefers_acceptance() -> None:
    acceptance = datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)
    available = resolve_available_at(
        acceptance_datetime=acceptance,
        filing_date=date(2024, 2, 15),
    )
    assert available == acceptance


def test_archived_submission_discovery(fixture_submissions: dict[str, object]) -> None:
    names = list_archive_filenames(fixture_submissions)
    assert names == ["CIK0000320193-submissions-001.json"]


def test_merge_includes_archived_filings(
    fixture_submissions: dict[str, object],
    fixture_archive: dict[str, object],
) -> None:
    merged = merge_submissions(fixture_submissions, [fixture_archive])
    accessions = merged["filings"]["recent"]["accessionNumber"]
    assert "0000320193-24-000050" in accessions
    assert "0000320193-21-000105" in accessions
    assert "0000320193-20-000096" in accessions


def test_merge_and_normalize_dedupe_overlapping_archive(
    fixture_submissions: dict[str, object],
) -> None:
    # Archive incorrectly repeats a recent accession; normalization must keep one.
    overlapping_archive = {
        "accessionNumber": ["0000320193-24-000050", "0000320193-19-000001"],
        "filingDate": ["2024-02-15", "2019-10-31"],
        "reportDate": ["2023-12-30", "2019-09-28"],
        "acceptanceDateTime": ["20240215163015", "20191031160000"],
        "form": ["10-Q", "10-K"],
        "fileNumber": ["001-36743", "001-36743"],
        "filmNumber": ["248765432", "191111111"],
        "primaryDocument": ["aapl-20231230.htm", "aapl-20190928.htm"],
        "isXBRL": [1, 1],
        "isInlineXBRL": [1, 1],
    }
    merged = merge_submissions(fixture_submissions, [overlapping_archive])
    filings = normalize_filings(merged)
    accessions = [f.accession_number for f in filings]
    assert accessions.count("0000320193-24-000050") == 1
    assert "0000320193-19-000001" in accessions


def test_normalize_issuer_and_security_separation(
    fixture_submissions: dict[str, object],
) -> None:
    issuer = normalize_issuer(fixture_submissions)
    securities = normalize_securities(
        fixture_submissions,
        resolved_ticker="AAPL",
        resolved_exchange="Nasdaq",
    )
    assert issuer.cik == "0000320193"
    assert issuer.legal_name == "Apple Inc."
    assert issuer.sic == "3571"
    assert len(securities) == 1
    assert securities[0].ticker == "AAPL"
    assert securities[0].cik == issuer.cik
    assert securities[0].is_primary is True
    assert not hasattr(issuer, "ticker")


def test_multiple_securities_one_issuer() -> None:
    submissions = {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL", "AAPL.W"],
        "exchanges": ["Nasdaq", "Nasdaq"],
        "filings": {"recent": {}},
    }
    issuer = normalize_issuer(submissions)
    securities = normalize_securities(submissions, resolved_ticker="AAPL")
    assert issuer.cik == "0000320193"
    assert {s.ticker for s in securities} == {"AAPL", "AAPL.W"}
    assert all(s.cik == issuer.cik for s in securities)
    assert sum(1 for s in securities if s.is_primary) == 1
    assert next(s for s in securities if s.ticker == "AAPL").is_primary is True


def test_amended_filing_keeps_original() -> None:
    submissions = {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-23-000106", "0000320193-23-000200"],
                "filingDate": ["2023-11-03", "2023-11-20"],
                "reportDate": ["2023-09-30", "2023-09-30"],
                "acceptanceDateTime": [
                    "2023-11-03T17:00:00.000Z",
                    "2023-11-20T12:00:00.000Z",
                ],
                "form": ["10-K", "10-K/A"],
                "primaryDocument": ["aapl-20230930.htm", "aapl-20230930-a.htm"],
                "isXBRL": [1, 1],
                "isInlineXBRL": [1, 1],
            }
        },
    }
    filings = normalize_filings(submissions)
    by_accn = {f.accession_number: f for f in filings}
    assert by_accn["0000320193-23-000106"].form == "10-K"
    assert by_accn["0000320193-23-000200"].form == "10-K/A"
    assert by_accn["0000320193-23-000106"].available_at == datetime(
        2023, 11, 3, 17, 0, 0, tzinfo=UTC
    )


def test_normalize_filings_timestamps(
    fixture_submissions: dict[str, object],
    fixture_archive: dict[str, object],
) -> None:
    merged = merge_submissions(fixture_submissions, [fixture_archive])
    filings = normalize_filings(merged)
    by_accn = {f.accession_number: f for f in filings}
    q1 = by_accn["0000320193-24-000050"]
    assert q1.acceptance_datetime == datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)
    assert q1.available_at == q1.acceptance_datetime

    old = by_accn["0000320193-20-000096"]
    assert old.acceptance_datetime is None
    assert old.available_at == datetime(2020, 10, 30, 23, 59, 59, tzinfo=EASTERN).astimezone(UTC)


def test_company_facts_normalization_and_dedup(
    fixture_facts: dict[str, object],
    fixture_submissions: dict[str, object],
    fixture_archive: dict[str, object],
) -> None:
    merged = merge_submissions(fixture_submissions, [fixture_archive])
    filings = {f.accession_number: f for f in normalize_filings(merged)}
    facts = normalize_company_facts(fixture_facts, filings_by_accession=filings)

    revenue = [f for f in facts if "Revenue" in f.concept and f.unit == "USD"]
    assert len(revenue) >= 2
    q1_revenue = [
        f
        for f in revenue
        if f.end_date == date(2023, 12, 30) and f.accession_number == "0000320193-24-000050"
    ]
    assert len(q1_revenue) == 1
    assert q1_revenue[0].value == 119575000000
    assert q1_revenue[0].available_at == datetime(2024, 2, 15, 21, 30, 15, tzinfo=UTC)
    assert any(f.unit == "USD/shares" for f in facts)
    assert any(f.concept == "NetIncomeLoss" and f.value == 33916000000 for f in facts)
    assert any(f.taxonomy == "dei" for f in facts)


def test_fact_uses_filing_acceptance_via_accession_join() -> None:
    filing = Filing(
        accession_number="0000320193-24-000050",
        cik="0000320193",
        form="10-Q",
        filing_date=date(2024, 2, 15),
        acceptance_datetime=datetime(2024, 2, 15, 16, 30, 15, tzinfo=UTC),
        available_at=datetime(2024, 2, 15, 16, 30, 15, tzinfo=UTC),
    )
    payload = {
        "cik": 320193,
        "facts": {
            "us-gaap": {
                "Assets": {
                    "label": "Assets",
                    "units": {
                        "USD": [
                            {
                                "end": "2023-12-30",
                                "val": 1,
                                "accn": "0000320193-24-000050",
                                "form": "10-Q",
                                "filed": "2024-02-15",
                            }
                        ]
                    },
                }
            }
        },
    }
    facts = normalize_company_facts(payload, filings_by_accession={filing.accession_number: filing})
    assert len(facts) == 1
    assert facts[0].available_at == datetime(2024, 2, 15, 16, 30, 15, tzinfo=UTC)
    assert facts[0].acceptance_datetime == filing.acceptance_datetime


def test_deduplicate_facts_helper(fixture_facts: dict[str, object]) -> None:
    facts = normalize_company_facts(fixture_facts)
    again = deduplicate_facts(facts + facts)
    assert len(again) == len(facts)
    assert {f.fact_id for f in again} == {f.fact_id for f in facts if f.fact_id}
