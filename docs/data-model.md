# Data model (v0.3a)

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


### market_instruments / market_symbol_mappings / daily_price_bars / market_data_runs

Additive v0.3a tables. `daily_price_bars` is unique on
`(instrument_id, provider, trading_date, adjustment_mode)` so raw and adjusted
series coexist. Prices use `DECIMAL(18, 6)` storage. `available_at` is the
exchange-local 16:15 convention converted to UTC; `fetched_at` records when
EquityTrace downloaded the bar.

`market_symbol_mappings` uses a surrogate `mapping_id` primary key so the same
provider symbol can be reused by a different instrument after a prior mapping
expires (`valid_from` / `valid_to`). Overlapping validity windows for the same
provider symbol across instruments are rejected. Child market tables intentionally
omit DuckDB foreign keys to `market_instruments` because DuckDB rejects parent-row
`UPDATE`s while FK children exist; application code preserves referential use of
`instrument_id`.

`market_instruments.canonical_symbol` remains `UNIQUE` in v0.3a: one active
display ticker per instrument. Historical ticker reuse across issuers is a
documented limitation; `instrument_id` is the durable key, but v0.3a still
derives it from the current display symbol.

SPY and other benchmarks can exist as ETF/index instruments without an SEC
issuer link. Equities preferably link to `securities` / `issuers` when present.

### Market response cache

Optional local cache under `EQUITYTRACE_MARKET_DATA_CACHE_DIR` (default
`data/cache/market`). Cache keys include provider, symbol, interval, date
range, and adjustment mode — never the API key. Only successful payloads with a
`values` list are cached; error payloads are not. There is no TTL in v0.3a:
disable `EQUITYTRACE_ENABLE_CACHE` to force fresh provider fetches when
corrections must be observed immediately. Corrupt cache files are ignored and
replaced.
