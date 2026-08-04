# Data model (v0.2)

## Conceptual model

| Entity | Identity | Notes |
| --- | --- | --- |
| Issuer | `cik` | SEC reporting entity / legal company |
| Security | `(ticker, cik)` | Traded instrument; many per issuer possible |
| Filing | `accession_number` | SEC submission with `available_at` |
| Financial fact | `fact_id` | Deterministic hash of natural key fields |
| Ingestion run | `run_id` | Audit trail for ingest operations |
| Factor run | `run_id` | Audit trail for factor calculations |
| Factor value | `(ticker, factor, fy, fp, as_of)` | Optional materialized factor output |

An issuer is **not** a ticker. Share classes and secondary listings are modeled as securities.

## Tables

### issuers / securities / filings / financial_facts / ingestion_runs

Unchanged from v0.1. Facts still preserve original `taxonomy` + `concept`.

### schema_migrations

Records applied logical schema versions (`0.2.0`, …). Initialization is
idempotent via `CREATE TABLE IF NOT EXISTS` plus `ON CONFLICT DO NOTHING`.

### factor_runs

One row per calculation attempt: ticker, CIK, factor name, fiscal period,
`as_of`, status.

### factor_values

Idempotent store keyed by `(ticker, factor_name, fiscal_year, fiscal_period, as_of)`.
Repeated calculations overwrite the stored value.

Canonical statement snapshots are **not** materialized by default; they are
assembled in memory by `FinancialsService`.

## Canonical concepts

Income, balance sheet, and cash-flow concepts are defined in
`equitytrace.financials.mappings.CANONICAL_MAPPINGS`. Derived metrics
(`free_cash_flow`, `net_debt`, `working_capital`, `invested_capital`, `ebitda`,
`nopat`) are computed only when required inputs exist.

## Availability semantics

`available_at` is the earliest timestamp a backtest may safely use the row.

- Prefer SEC acceptance datetime (Eastern → UTC)
- Else end of filing date (Eastern → UTC)
- Never start-of-day filing date
- Never report period end date

Amendments (`10-K/A`, `10-Q/A`) are separate facts. A snapshot before the
amendment acceptance uses the original filing; after acceptance, the amended
value may win when it is the best available fact.

## Naming note (EquityTrace rename)

Table names and persisted migration identifiers do not embed the product brand.
Databases created under the historical FilingEdge default path
(`data/filingedge.duckdb`) remain compatible when opened via an explicit
`EQUITYTRACE_DATABASE_PATH` (or temporary `FILINGEDGE_DATABASE_PATH`) setting.
