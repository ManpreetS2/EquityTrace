# Data model (v0.3.2.dev0)

Latest release is **v0.3.1**. This document describes the live schema on the
`0.3.2.dev0` development line.

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
| Market instrument | `instrument_id` | Durable market identity; display symbol is unique in v0.3a |
| Daily price bar | `(instrument_id, provider, trading_date, adjustment_mode)` | Raw and adjusted series coexist |
| Portfolio run | `run_id` | Native research backtest (v0.3c) |

An issuer is **not** a ticker. Share classes and secondary listings are modeled as securities.

## Tables

### issuers / securities / filings / financial_facts / ingestion_runs

Unchanged from v0.1. Facts still preserve original `taxonomy` + `concept`.

### schema_migrations

Records applied logical schema versions. Initialization is idempotent via
`CREATE TABLE IF NOT EXISTS` plus `ON CONFLICT DO NOTHING`.

Current labels: `0.2.0`, `0.3.0-a` (legacy alias `0.3.0a`), `0.3.0-a1`,
`0.3.0-a2`, `0.3.0-b`, `0.3.0-c`.

### factor_runs

One row per calculation attempt: ticker, CIK, factor name, fiscal period,
`as_of`, status.

### factor_values

Idempotent store keyed by `(ticker, factor_name, fiscal_year, fiscal_period, as_of)`.
Repeated calculations overwrite the stored value.

Nullable `market_input_json` (migration `0.3.0-b`) stores valuation market-input
provenance when a multiple used PIT market cap: date, knowledge time, raw close,
shares, currency, and whether the cap was stored data or a manual override.
Non-valuation factors leave the column null.

Canonical statement snapshots are **not** materialized by default; they are
assembled in memory by `FinancialsService`.

## Canonical concepts

Income, balance sheet, and cash-flow concepts are defined in
`equitytrace.financials.mappings.CANONICAL_MAPPINGS`. Derived metrics
(`free_cash_flow`, `net_debt`, `working_capital`, `invested_capital`, `ebitda`,
`nopat`) are computed only when required inputs exist.

## Availability semantics

`available_at` is the earliest timestamp a backtest may safely use the row.

SEC acceptance:

- timestamps with `Z` or an explicit offset are absolute instants (stored UTC)
- naive acceptance timestamps are U.S. Eastern wall time, then converted to UTC
- missing or date-only acceptance uses filing-date end-of-day Eastern fallback

Never start-of-day filing date. Never report period end date.

Amendments (`10-K/A`, `10-Q/A`) are separate facts. A snapshot before the
amendment `available_at` uses the original filing; after that instant, the
amended value may win when it is the best available fact.

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
`values` list are cached; error payloads are not. There is no TTL: disable
`EQUITYTRACE_ENABLE_CACHE` to force fresh provider fetches when corrections must
be observed immediately. Corrupt cache files are ignored and replaced.

### portfolio_runs / portfolio_rebalances / portfolio_weight_transitions / portfolio_equity

Additive v0.3c tables (migration `0.3.0-c`). They persist native weight-return
research backtests. No fill prices, share quantities, or order ids.

- `portfolio_runs` — config, status, warnings, audit JSON, final NAV, metrics.
  `adjustment_mode` is persisted for provenance; v0.3c native backtests currently
  require `adjustment_mode=all`.
- `portfolio_rebalances` — PK `(run_id, decision_at)`; unique
  `(run_id, target_effective_at)`; turnover and cost
- `portfolio_weight_transitions` — PK `(run_id, decision_at, symbol)`; drifted
  vs target weights, notional, allocated cost, signal provenance (including
  valuation `market_input` when present)
- `portfolio_equity` — PK `(run_id, valuation_at)`; one post-event row per
  valuation session (NAV, cash weight, drawdown, optional benchmark NAV)

Writes are one transaction per completed run. A first-rebalance unavailable
result is still persisted with that status; later unavailable rebalances keep
zero turnover/cost. The request keeps the caller's original tickers.
Multiple universe securities that share one issuer CIK make the run
unavailable rather than selecting a share class.
