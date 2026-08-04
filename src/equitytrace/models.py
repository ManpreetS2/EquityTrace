"""Domain models for issuers, securities, filings, and financial facts."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    from datetime import UTC

    return datetime.now(UTC)


class Issuer(BaseModel):
    """An SEC reporting entity (company), identified by CIK."""

    model_config = ConfigDict(frozen=True)

    cik: str
    legal_name: str
    entity_type: str | None = None
    sic: str | None = None
    sic_description: str | None = None
    fiscal_year_end: str | None = None
    state_of_incorporation: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("cik")
    @classmethod
    def _normalize_cik(cls, value: str) -> str:
        return normalize_cik(value)


class Security(BaseModel):
    """A traded security associated with an SEC issuer."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    cik: str
    title: str | None = None
    exchange: str | None = None
    is_primary: bool = False
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("cik")
    @classmethod
    def _normalize_cik(cls, value: str) -> str:
        return normalize_cik(value)


class Filing(BaseModel):
    """A single SEC submission/filing."""

    model_config = ConfigDict(frozen=True)

    accession_number: str
    cik: str
    form: str
    filing_date: date
    report_date: date | None = None
    acceptance_datetime: datetime | None = None
    available_at: datetime
    primary_document: str | None = None
    file_number: str | None = None
    film_number: str | None = None
    is_xbrl: bool = False
    is_inline_xbrl: bool = False
    source_url: str | None = None

    @field_validator("cik")
    @classmethod
    def _normalize_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @field_validator("accession_number")
    @classmethod
    def _normalize_accession(cls, value: str) -> str:
        return value.strip()


class FinancialFact(BaseModel):
    """A single XBRL financial fact with point-in-time availability."""

    model_config = ConfigDict(frozen=True)

    cik: str
    taxonomy: str
    concept: str
    label: str | None = None
    description: str | None = None
    unit: str
    value: float
    start_date: date | None = None
    end_date: date | None = None
    filing_date: date | None = None
    acceptance_datetime: datetime | None = None
    available_at: datetime
    accession_number: str | None = None
    form: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    frame: str | None = None
    source_url: str | None = None
    fact_id: str | None = None

    @field_validator("cik")
    @classmethod
    def _normalize_cik(cls, value: str) -> str:
        return normalize_cik(value)

    def with_fact_id(self) -> FinancialFact:
        """Return a copy with a deterministic natural key."""
        return self.model_copy(update={"fact_id": compute_fact_id(self)})


class IngestionRun(BaseModel):
    """Metadata for a single company ingestion run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    ticker: str
    cik: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    issuer_count: int = 0
    security_count: int = 0
    filing_count: int = 0
    fact_count: int = 0
    error_message: str | None = None


class ResolvedTicker(BaseModel):
    """Ticker resolution result."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    cik: str
    company_name: str
    exchange: str | None = None


class IngestResult(BaseModel):
    """Summary of a successful company ingestion."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    cik: str
    legal_name: str
    issuer_count: int
    security_count: int
    filing_count: int
    fact_count: int
    elapsed_seconds: float


def normalize_cik(value: str | int) -> str:
    """Normalize a CIK to a zero-padded 10-digit string."""
    text = str(value).strip()
    if text.isdigit():
        return text.zfill(10)
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        raise ValueError(f"Invalid CIK: {value!r}")
    return digits.zfill(10)


def compute_fact_id(fact: FinancialFact | dict[str, Any]) -> str:
    """Compute a deterministic ID that uniquely identifies a financial fact."""
    if isinstance(fact, FinancialFact):
        parts = [
            fact.cik,
            fact.taxonomy,
            fact.concept,
            fact.unit,
            fact.start_date.isoformat() if fact.start_date else "",
            fact.end_date.isoformat() if fact.end_date else "",
            fact.accession_number or "",
            fact.form or "",
            str(fact.fiscal_year or ""),
            fact.fiscal_period or "",
            fact.frame or "",
            f"{fact.value:.10g}",
        ]
    else:
        parts = [
            str(fact.get("cik", "")),
            str(fact.get("taxonomy", "")),
            str(fact.get("concept", "")),
            str(fact.get("unit", "")),
            str(fact.get("start_date") or ""),
            str(fact.get("end_date") or ""),
            str(fact.get("accession_number") or ""),
            str(fact.get("form") or ""),
            str(fact.get("fiscal_year") or ""),
            str(fact.get("fiscal_period") or ""),
            str(fact.get("frame") or ""),
            f"{float(fact['value']):.10g}" if fact.get("value") is not None else "",
        ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]
