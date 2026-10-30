"""Synthetic demo dataset: determinism, identity, chronology and labelling."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from equitytrace.demo.data import (
    CALENDAR_SYMBOL,
    COMPANIES,
    DATASET_VERSION,
    PRICE_END,
    PRICE_START,
    DemoDataset,
    build_dataset,
    trading_sessions,
)

EASTERN = ZoneInfo("America/New_York")
# Pinned on purpose: any change to the synthetic content must be deliberate and bump
# DATASET_VERSION so existing demo databases are recognised as stale.
PINNED_DIGEST = "6f360c8fa4919e19ab9f782f17688c7d480552f87d50b2ad3eafba01a115a8c7"


@pytest.fixture(scope="module")
def dataset() -> DemoDataset:
    return build_dataset()


def test_digest_is_deterministic_and_pinned(dataset: DemoDataset) -> None:
    assert build_dataset().digest() == dataset.digest()
    assert dataset.digest() == PINNED_DIGEST, (
        "Dataset content changed. If intentional, bump DATASET_VERSION and re-pin."
    )
    assert DATASET_VERSION == "1"


@pytest.mark.parametrize("seed", ["0", "1", "42"])
def test_digest_is_independent_of_hash_seed(seed: str) -> None:
    code = "from equitytrace.demo.data import build_dataset; print(build_dataset().digest())"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": str(root / "src"),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        },
    )
    assert result.stdout.strip() == PINNED_DIGEST


def test_universe_shape_and_stable_identifiers(dataset: DemoDataset) -> None:
    assert dataset.tickers == ("SYAL", "SYBR", "SYCD", "SYDL", "SYEN", "SYFM", "SYGA", "SYGB")
    assert 6 <= len(dataset.tickers) <= 8
    ciks = [company.issuer.cik for company in dataset.companies]
    assert ciks == [f"099000000{i}" for i in range(1, 8)]
    # A range the SEC has never assigned: nothing here can pass for a real issuer.
    assert all(cik.startswith("099") for cik in ciks)
    for company in dataset.companies:
        assert "(Synthetic)" in company.issuer.legal_name
        assert all(s.cik == company.issuer.cik for s in company.securities)
    grove = next(c for c in dataset.companies if c.spec.ticker == "SYGA")
    assert [s.ticker for s in grove.securities] == ["SYGA", "SYGB"]
    assert [s.is_primary for s in grove.securities] == [True, False]


def test_facts_have_unique_traceable_identity_and_provenance(dataset: DemoDataset) -> None:
    ids: set[str] = set()
    natural: set[tuple[object, ...]] = set()
    for company in dataset.companies:
        accessions = {f.accession_number for f in company.filings}
        assert len(accessions) == 3
        for fact in company.facts:
            assert fact.cik == company.issuer.cik
            assert fact.fact_id and fact.fact_id not in ids
            ids.add(fact.fact_id)
            key = (fact.cik, fact.concept, fact.end_date, fact.accession_number)
            assert key not in natural
            natural.add(key)
            assert fact.accession_number in accessions
            assert fact.source_url and fact.source_url.startswith("synthetic://")
            assert fact.label and fact.label.startswith("SYNTHETIC")
            assert fact.description and "not an SEC filing" in fact.description
            assert fact.taxonomy == "us-gaap" and fact.unit == "USD"
            assert fact.form == "10-K" and fact.fiscal_period == "FY"
    assert len(ids) == dataset.fact_count == 168


def test_fiscal_chronology_is_plausible_and_never_precedes_the_period(
    dataset: DemoDataset,
) -> None:
    for company in dataset.companies:
        filings = {f.accession_number: f for f in company.filings}
        for fact in company.facts:
            assert fact.fiscal_year is not None and fact.end_date is not None
            assert fact.end_date == date(fact.fiscal_year, 12, 31)
            if fact.start_date is not None:
                assert fact.start_date == date(fact.fiscal_year, 1, 1)
            assert fact.filing_date is not None and fact.filing_date > fact.end_date
            assert fact.filing_date.year == fact.fiscal_year + 1
            assert 1 <= (fact.filing_date - fact.end_date).days <= 100
            period_close = datetime.combine(fact.end_date, time(23, 59, 59), tzinfo=EASTERN)
            assert fact.available_at > period_close
            assert fact.acceptance_datetime == fact.available_at
            assert fact.available_at.astimezone(EASTERN).date() == fact.filing_date
            filing = filings[fact.accession_number or ""]
            assert filing.available_at == fact.available_at
            assert filing.report_date == fact.end_date


def test_filing_dates_are_staggered_so_cross_sections_mix_years(dataset: DemoDataset) -> None:
    fy2023 = {c.spec.ticker: c.filings[-1].filing_date for c in dataset.companies}
    assert len(set(fy2023.values())) >= 6
    assert min(fy2023.values()) < date(2024, 2, 20) < max(fy2023.values())


def test_prices_are_aligned_valid_and_pit_stamped(dataset: DemoDataset) -> None:
    sessions = trading_sessions()
    assert sessions[0] >= PRICE_START and sessions[-1] <= PRICE_END
    assert all(day.weekday() < 5 for day in sessions)
    assert date(2023, 7, 4) not in sessions and date(2023, 12, 25) not in sessions
    assert 600 <= len(sessions) <= 640
    symbols = {s.symbol for s in dataset.series}
    assert symbols == {CALENDAR_SYMBOL, *dataset.tickers}
    for series in dataset.series:
        assert [bar.trading_date for bar in series.bars] == list(sessions)
        for bar in series.bars:
            assert bar.close > 0 and bar.low <= min(bar.open, bar.close)
            assert bar.high >= max(bar.open, bar.close) and isinstance(bar.close, Decimal)
            assert bar.available_at.astimezone(EASTERN).time() == time(16, 15)
            assert bar.available_at.astimezone(EASTERN).date() == bar.trading_date
            assert bar.fetched_at > bar.available_at
            assert bar.source_metadata_json and '"synthetic": true' in bar.source_metadata_json
    by_symbol = {s.symbol: s for s in dataset.series}
    assert by_symbol[CALENDAR_SYMBOL].issuer_cik is None
    assert by_symbol["SYGA"].issuer_cik == by_symbol["SYGB"].issuer_cik
    assert by_symbol["SYGA"].bars[-1].close != by_symbol["SYGB"].bars[-1].close


def test_company_specs_describe_distinct_fundamentals() -> None:
    roa = {
        spec.ticker: spec.years[2023].net_income / spec.years[2023].total_assets
        for spec in COMPANIES
    }
    assert len({round(value, 6) for value in roa.values()}) == len(COMPANIES)
