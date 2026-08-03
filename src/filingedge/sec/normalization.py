"""Normalize SEC JSON payloads into typed domain records."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from filingedge.models import (
    Filing,
    FinancialFact,
    Issuer,
    Security,
    compute_fact_id,
    normalize_cik,
)
from filingedge.sec.client import SUBMISSIONS_URL

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


def parse_acceptance_datetime(value: str | None) -> datetime | None:
    """
    Parse an SEC acceptance timestamp into a timezone-aware UTC datetime.

    Supported forms:
    - ISO-8601 with ``Z`` or an explicit offset (as returned by data.sec.gov
      submissions JSON). These are absolute instants and are converted to UTC.
    - Compact ``YYYYMMDDHHMMSS`` with no zone marker. Treated as U.S. Eastern
      wall time (EST/EDT via ``America/New_York``), then converted to UTC.
    - Naive ISO datetimes without a zone marker. Treated as U.S. Eastern wall
      time, then converted to UTC.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Prefer explicit-zone ISO first. Live SEC submissions use values such as
    # ``2024-02-15T16:30:15.000Z`` (UTC). Never reinterpret those as Eastern.
    if _has_explicit_zone(text):
        try:
            parsed = datetime.fromisoformat(_normalize_iso_zone(text))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
        return parsed.replace(tzinfo=EASTERN).astimezone(UTC)

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=EASTERN)
            return parsed.astimezone(UTC)
        except ValueError:
            continue

    compact = text.replace("-", "").replace(":", "").replace("T", "").replace(" ", "")
    if compact.endswith(("Z", "z")):
        # Zone present but ISO parse failed above; do not treat as Eastern.
        logger.debug("Unrecognized zoned acceptance datetime format: %r", value)
        return None
    if len(compact) >= 14 and compact[:14].isdigit():
        eastern = datetime.strptime(compact[:14], "%Y%m%d%H%M%S").replace(tzinfo=EASTERN)
        return eastern.astimezone(UTC)
    if len(compact) >= 8 and compact[:8].isdigit() and len(compact) < 14:
        # Date-only style acceptance values are ambiguous; treat conservatively later.
        return None

    logger.debug("Unrecognized acceptance datetime format: %r", value)
    return None


def _has_explicit_zone(text: str) -> bool:
    if text.endswith(("Z", "z")):
        return True
    # Offset after the date portion, e.g. ...+00:00 or ...-05:00
    return len(text) >= 19 and ("+" in text[10:] or text.count("-") >= 3)


def _normalize_iso_zone(text: str) -> str:
    if text.endswith(("Z", "z")):
        return text[:-1] + "+00:00"
    return text


def end_of_eastern_day_utc(day: date) -> datetime:
    """Return end-of-day U.S. Eastern for ``day``, converted to UTC."""
    eastern_eod = datetime.combine(day, time(23, 59, 59), tzinfo=EASTERN)
    return eastern_eod.astimezone(UTC)


def resolve_available_at(
    *,
    acceptance_datetime: datetime | None,
    filing_date: date | None,
) -> datetime:
    """
    Determine the earliest safe public-availability timestamp.

    Prefer the SEC acceptance datetime. If unavailable, use the conservative
    end of the filing date in U.S. Eastern time (converted to UTC). Do not
    assume the filing was public at the beginning of the filing date.
    """
    if acceptance_datetime is not None:
        if acceptance_datetime.tzinfo is None:
            # Naive acceptance values are treated as Eastern wall time.
            return acceptance_datetime.replace(tzinfo=EASTERN).astimezone(UTC)
        return acceptance_datetime.astimezone(UTC)
    if filing_date is not None:
        return end_of_eastern_day_utc(filing_date)
    # Absolute last resort: far-future would hide data; far-past would leak.
    # Use a clearly late sentinel only when both values are missing.
    return datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def normalize_issuer(submissions: dict[str, Any], *, cik: str | None = None) -> Issuer:
    """Normalize issuer metadata from a submissions payload."""
    resolved_cik = normalize_cik(cik or submissions.get("cik") or "")
    return Issuer(
        cik=resolved_cik,
        legal_name=str(submissions.get("name") or "").strip() or f"CIK {resolved_cik}",
        entity_type=_optional_str(submissions.get("entityType")),
        sic=_optional_str(submissions.get("sic")),
        sic_description=_optional_str(submissions.get("sicDescription")),
        fiscal_year_end=_optional_str(submissions.get("fiscalYearEnd")),
        state_of_incorporation=_optional_str(
            submissions.get("stateOfIncorporation")
            or submissions.get("stateOfIncorporationDescription")
        ),
    )


def normalize_securities(
    submissions: dict[str, Any],
    *,
    resolved_ticker: str | None = None,
    resolved_exchange: str | None = None,
) -> list[Security]:
    """
    Normalize traded securities for an issuer.

    Tickers and issuers are separate concepts: one CIK may map to multiple
    tickers / share classes.
    """
    cik = normalize_cik(submissions.get("cik") or "")
    tickers = submissions.get("tickers") or []
    exchanges = submissions.get("exchanges") or []
    if not isinstance(tickers, list):
        tickers = []
    if not isinstance(exchanges, list):
        exchanges = []

    securities: list[Security] = []
    seen: set[str] = set()
    for idx, ticker_value in enumerate(tickers):
        ticker = str(ticker_value or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        exchange = None
        if idx < len(exchanges):
            exchange = _optional_str(exchanges[idx])
        securities.append(
            Security(
                ticker=ticker,
                cik=cik,
                title=_optional_str(submissions.get("name")),
                exchange=exchange,
                is_primary=resolved_ticker is not None and ticker == resolved_ticker.upper(),
            )
        )

    if resolved_ticker:
        ticker = resolved_ticker.strip().upper()
        if ticker not in seen:
            securities.insert(
                0,
                Security(
                    ticker=ticker,
                    cik=cik,
                    title=_optional_str(submissions.get("name")),
                    exchange=resolved_exchange,
                    is_primary=True,
                ),
            )
        else:
            securities = [
                s.model_copy(
                    update={
                        "is_primary": s.ticker == ticker,
                        "exchange": s.exchange
                        or (resolved_exchange if s.ticker == ticker else None),
                    }
                )
                for s in securities
            ]

    if not securities and resolved_ticker:
        securities.append(
            Security(
                ticker=resolved_ticker.strip().upper(),
                cik=cik,
                title=_optional_str(submissions.get("name")),
                exchange=resolved_exchange,
                is_primary=True,
            )
        )
    return securities


def normalize_filings(submissions: dict[str, Any]) -> list[Filing]:
    """Normalize filings from a (possibly merged) submissions payload."""
    cik = normalize_cik(submissions.get("cik") or "")
    recent = submissions.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return []

    accession_numbers = recent.get("accessionNumber") or []
    if not isinstance(accession_numbers, list) or not accession_numbers:
        return []

    n = len(accession_numbers)
    forms = _pad_list(recent.get("form"), n)
    filing_dates = _pad_list(recent.get("filingDate"), n)
    report_dates = _pad_list(recent.get("reportDate"), n)
    acceptance_datetimes = _pad_list(recent.get("acceptanceDateTime"), n)
    primary_docs = _pad_list(recent.get("primaryDocument"), n)
    file_numbers = _pad_list(recent.get("fileNumber"), n)
    film_numbers = _pad_list(recent.get("filmNumber"), n)
    is_xbrl_flags = _pad_list(recent.get("isXBRL"), n)
    is_inline_flags = _pad_list(recent.get("isInlineXBRL"), n)

    filings: list[Filing] = []
    seen: set[str] = set()
    for i, accession in enumerate(accession_numbers):
        accession_number = str(accession or "").strip()
        if not accession_number or accession_number in seen:
            continue
        filing_date = _parse_date(filing_dates[i])
        if filing_date is None:
            logger.debug("Skipping filing without filingDate: %s", accession_number)
            continue
        acceptance = parse_acceptance_datetime(
            str(acceptance_datetimes[i]) if acceptance_datetimes[i] is not None else None
        )
        available_at = resolve_available_at(
            acceptance_datetime=acceptance,
            filing_date=filing_date,
        )
        form = str(forms[i] or "").strip() or "UNKNOWN"
        seen.add(accession_number)
        filings.append(
            Filing(
                accession_number=accession_number,
                cik=cik,
                form=form,
                filing_date=filing_date,
                report_date=_parse_date(report_dates[i]),
                acceptance_datetime=acceptance,
                available_at=available_at,
                primary_document=_optional_str(primary_docs[i]),
                file_number=_optional_str(file_numbers[i]),
                film_number=_optional_str(film_numbers[i]),
                is_xbrl=_as_bool(is_xbrl_flags[i]),
                is_inline_xbrl=_as_bool(is_inline_flags[i]),
                source_url=SUBMISSIONS_URL.format(cik=cik),
            )
        )
    return filings


def normalize_company_facts(
    company_facts: dict[str, Any],
    *,
    filings_by_accession: dict[str, Filing] | None = None,
) -> list[FinancialFact]:
    """
    Normalize XBRL Company Facts into typed financial facts.

    Preserves original taxonomy/concept names. Handles multiple units, instant
    and duration facts, missing fields, and duplicate rows.
    """
    cik = normalize_cik(company_facts.get("cik") or "")
    facts_root = company_facts.get("facts") or {}
    if not isinstance(facts_root, dict):
        return []

    filing_lookup = filings_by_accession or {}
    rows: list[dict[str, Any]] = []

    for taxonomy, concepts in facts_root.items():
        if not isinstance(concepts, dict):
            continue
        for concept, concept_payload in concepts.items():
            if not isinstance(concept_payload, dict):
                continue
            label = _optional_str(concept_payload.get("label"))
            description = _optional_str(concept_payload.get("description"))
            units = concept_payload.get("units") or {}
            if not isinstance(units, dict):
                continue
            for unit, observations in units.items():
                if not isinstance(observations, list):
                    continue
                for obs in observations:
                    if not isinstance(obs, dict):
                        continue
                    row = _normalize_observation(
                        cik=cik,
                        taxonomy=str(taxonomy),
                        concept=str(concept),
                        label=label,
                        description=description,
                        unit=str(unit),
                        obs=obs,
                        filing_lookup=filing_lookup,
                    )
                    if row is not None:
                        rows.append(row)

    if not rows:
        return []

    # Explicit schema is required: live SEC payloads mix nulls and values in ways
    # that break Polars' default short-window schema inference.
    frame = pl.DataFrame(
        rows,
        schema={
            "cik": pl.String,
            "taxonomy": pl.String,
            "concept": pl.String,
            "label": pl.String,
            "description": pl.String,
            "unit": pl.String,
            "value": pl.Float64,
            "start_date": pl.String,
            "end_date": pl.String,
            "filing_date": pl.String,
            "acceptance_datetime": pl.String,
            "available_at": pl.String,
            "accession_number": pl.String,
            "form": pl.String,
            "fiscal_year": pl.Int64,
            "fiscal_period": pl.String,
            "frame": pl.String,
            "source_url": pl.String,
            "fact_id": pl.String,
        },
    )
    # Deduplicate by deterministic natural key, keeping the earliest available_at.
    frame = (
        frame.sort("available_at")
        .unique(subset=["fact_id"], keep="first")
        .sort(["concept", "end_date", "available_at"], descending=[False, True, True])
    )

    facts: list[FinancialFact] = []
    for record in frame.to_dicts():
        facts.append(
            FinancialFact(
                cik=record["cik"],
                taxonomy=record["taxonomy"],
                concept=record["concept"],
                label=record.get("label"),
                description=record.get("description"),
                unit=record["unit"],
                value=float(record["value"]),
                start_date=_parse_date(record.get("start_date")),
                end_date=_parse_date(record.get("end_date")),
                filing_date=_parse_date(record.get("filing_date")),
                acceptance_datetime=_parse_iso_datetime(record.get("acceptance_datetime")),
                available_at=_parse_iso_datetime(record["available_at"])
                or resolve_available_at(acceptance_datetime=None, filing_date=None),
                accession_number=record.get("accession_number"),
                form=record.get("form"),
                fiscal_year=record.get("fiscal_year"),
                fiscal_period=record.get("fiscal_period"),
                frame=record.get("frame"),
                source_url=record.get("source_url"),
                fact_id=record["fact_id"],
            )
        )
    return facts


def deduplicate_facts(facts: list[FinancialFact]) -> list[FinancialFact]:
    """Remove duplicate facts using deterministic fact IDs."""
    seen: set[str] = set()
    unique: list[FinancialFact] = []
    for fact in facts:
        identified = fact if fact.fact_id else fact.with_fact_id()
        fact_id = identified.fact_id or compute_fact_id(identified)
        if fact_id in seen:
            continue
        seen.add(fact_id)
        unique.append(identified)
    return unique


def _normalize_observation(
    *,
    cik: str,
    taxonomy: str,
    concept: str,
    label: str | None,
    description: str | None,
    unit: str,
    obs: dict[str, Any],
    filing_lookup: dict[str, Filing],
) -> dict[str, Any] | None:
    value = _parse_numeric(obs.get("val"))
    if value is None:
        return None

    end_date = _parse_date(obs.get("end"))
    start_date = _parse_date(obs.get("start"))
    filed = _parse_date(obs.get("filed"))
    accession = _optional_str(obs.get("accn"))
    form = _optional_str(obs.get("form"))
    fiscal_year = _parse_int(obs.get("fy"))
    fiscal_period = _optional_str(obs.get("fp"))
    frame = _optional_str(obs.get("frame"))

    acceptance: datetime | None = None
    filing_date = filed
    if accession and accession in filing_lookup:
        filing = filing_lookup[accession]
        acceptance = filing.acceptance_datetime
        filing_date = filing.filing_date or filed
        form = form or filing.form

    available_at = resolve_available_at(
        acceptance_datetime=acceptance,
        filing_date=filing_date,
    )

    provisional = FinancialFact(
        cik=cik,
        taxonomy=taxonomy,
        concept=concept,
        label=label,
        description=description,
        unit=unit,
        value=value,
        start_date=start_date,
        end_date=end_date,
        filing_date=filing_date,
        acceptance_datetime=acceptance,
        available_at=available_at,
        accession_number=accession,
        form=form,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        frame=frame,
        source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
    )
    identified = provisional.with_fact_id()
    return {
        "cik": identified.cik,
        "taxonomy": identified.taxonomy,
        "concept": identified.concept,
        "label": identified.label,
        "description": identified.description,
        "unit": identified.unit,
        "value": identified.value,
        "start_date": identified.start_date.isoformat() if identified.start_date else None,
        "end_date": identified.end_date.isoformat() if identified.end_date else None,
        "filing_date": identified.filing_date.isoformat() if identified.filing_date else None,
        "acceptance_datetime": (
            identified.acceptance_datetime.isoformat() if identified.acceptance_datetime else None
        ),
        "available_at": identified.available_at.isoformat(),
        "accession_number": identified.accession_number,
        "form": identified.form,
        "fiscal_year": identified.fiscal_year,
        "fiscal_period": identified.fiscal_period,
        "frame": identified.frame,
        "source_url": identified.source_url,
        "fact_id": identified.fact_id,
    }


def _pad_list(values: object, n: int) -> list[Any]:
    if not isinstance(values, list):
        return [None] * n
    if len(values) >= n:
        return list(values[:n])
    return list(values) + [None] * (n - len(values))


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_iso_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return parse_acceptance_datetime(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_numeric(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False
