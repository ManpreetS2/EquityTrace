# Data model (v0.1)

## Conceptual model

| Entity | Identity | Notes |
| --- | --- | --- |
| Issuer | `cik` | SEC reporting entity / legal company |
| Security | `(ticker, cik)` | Traded instrument; many per issuer possible |
| Filing | `accession_number` | SEC submission with `available_at` |
| Financial fact | `fact_id` | Deterministic hash of natural key fields |
| Ingestion run | `run_id` | Audit trail for ingest operations |

An issuer is **not** a ticker. Share classes and secondary listings are modeled as securities.

## Tables

### issuers

`cik`, `legal_name`, `entity_type`, `sic`, `sic_description`, `fiscal_year_end`, `state_of_incorporation`, timestamps

### securities

`ticker`, `cik`, `title`, `exchange`, `is_primary`, timestamps
Primary key: `(ticker, cik)`

### filings

`accession_number`, `cik`, `form`, `filing_date`, `report_date`, `acceptance_datetime`, `available_at`, document metadata, XBRL flags, `source_url`

### financial_facts

Preserves original `taxonomy` + `concept` (no canonical COA mapping in v0.1).

Natural key components hashed into `fact_id`:

- cik, taxonomy, concept, unit
- start_date, end_date
- accession_number, form, fiscal_year, fiscal_period, frame
- value

Duplicate observations from repeated ingestion collapse on `fact_id`.

### ingestion_runs

Tracks success/failure counts and timestamps for each ingest attempt.

## Availability semantics

`available_at` is the earliest timestamp a backtest may safely use the row.

- Prefer SEC acceptance datetime (Eastern → UTC)
- Else end of filing date (Eastern → UTC)
- Never start-of-day filing date
- Never report period end date
