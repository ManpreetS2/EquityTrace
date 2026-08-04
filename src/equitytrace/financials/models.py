"""Typed models for canonical values, provenance, and snapshots."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FinancialPeriod(BaseModel):
    """A fiscal reporting period request."""

    model_config = ConfigDict(frozen=True)

    fiscal_year: int
    fiscal_period: str
    kind: Literal["annual", "quarterly"]

    def label(self) -> str:
        """Human-readable period label used in CLI output."""
        if self.kind == "annual":
            return f"FY{self.fiscal_year}"
        return f"{self.fiscal_period}-{self.fiscal_year}"


class ValueProvenance(BaseModel):
    """Evidence describing how a canonical value was obtained."""

    model_config = ConfigDict(frozen=True)

    source_concept: str | None = None
    taxonomy: str | None = None
    unit: str | None = None
    accession_number: str | None = None
    form: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    filing_date: date | None = None
    acceptance_datetime: datetime | None = None
    available_at: datetime | None = None
    fact_id: str | None = None
    is_derived: bool = False
    derivation: str | None = None
    derivation_inputs: tuple[str, ...] = ()
    derivation_accessions: tuple[str, ...] = ()
    mapping_priority: int | None = None
    is_restated: bool = False
    reported_value: float | None = None
    sign_mode: str | None = None
    normalized_from_sign: bool = False


class CanonicalValue(BaseModel):
    """A single normalized statement value with provenance."""

    model_config = ConfigDict(frozen=True)

    concept: str
    value: float | None
    unit: str | None = None
    provenance: ValueProvenance = Field(default_factory=ValueProvenance)
    warnings: tuple[str, ...] = ()
    missing_reason: str | None = None

    @property
    def is_present(self) -> bool:
        return self.value is not None


class FinancialSnapshot(BaseModel):
    """Point-in-time canonical financial snapshot for one period."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    cik: str
    period: FinancialPeriod
    as_of: datetime
    period_start: date | None = None
    period_end: date | None = None
    income_statement: dict[str, CanonicalValue] = Field(default_factory=dict)
    balance_sheet: dict[str, CanonicalValue] = Field(default_factory=dict)
    cash_flow: dict[str, CanonicalValue] = Field(default_factory=dict)
    derived: dict[str, CanonicalValue] = Field(default_factory=dict)
    source_accessions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    def get_value(self, concept: str) -> CanonicalValue | None:
        """Lookup a concept across statement sections."""
        for section in (
            self.income_statement,
            self.balance_sheet,
            self.cash_flow,
            self.derived,
        ):
            if concept in section:
                return section[concept]
        return None

    def require_number(self, concept: str) -> float | None:
        """Return a numeric value when present."""
        item = self.get_value(concept)
        if item is None or item.value is None:
            return None
        return float(item.value)
