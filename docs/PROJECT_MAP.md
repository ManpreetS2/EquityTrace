# EquityTrace Project Map

Read this file first before repository work. Inspect only the bounded
subsystem named here before searching the rest of the repository.

Any PR that materially changes module locations, responsibilities, persistence
tables, runtime data flows, critical invariants, or test ownership must update
this map in the same PR.

## Snapshot status

| Field | Value |
| --- | --- |
| Package | `equitytrace` `0.3.2.dev0` |
| Release target | `v0.3.1` released; native v0.3c backtest foundation in review |
| v0.3a | complete (v0.3.0) |
| v0.3b | complete (v0.3.1) |
| Next milestone | remainder of v0.3c — skfolio min-variance adapter |
| Default branch | `main` |
| Storage | DuckDB (idempotent `CREATE IF NOT EXISTS` + additive repair helpers) |
| Package layout | `src/equitytrace/` |
| Not present today | skfolio optimizer adapter (v0.3c PR 2) |

This map describes files that exist in the live tree. Planned packages are
labeled **future** and must not be treated as implemented.

## Product boundary

EquityTrace is a Python-first quantitative **research** platform:

* SEC/XBRL ingestion
* point-in-time financial statements
* traceable normalized facts
* transparent fundamental factors
* market data (v0.3a)
* valuation factors + market-window analytics (v0.3b)
* native weight-return research backtests (v0.3c)
* reproducible rankings

It is **not** an AI stock picker, personalized advice engine, black-box scorer,
broker, or live-trading system.

If financial or data semantics are ambiguous, return `UNAVAILABLE` plus an
explanation. Do not fabricate a confident result.

## Repository at a glance

```text
EquityTrace/
├── src/equitytrace/          # library + CLI
│   ├── cli.py                # SEC / statements / factors CLI
│   ├── cli_market.py         # market ingest / prices / cap / analytics CLI
│   ├── cli_portfolio.py      # portfolio backtest CLI
│   ├── config.py             # EQUITYTRACE_* settings (+ FILINGEDGE_* fallback)
│   ├── database.py           # schema, init, v0.2 / v0.3a / v0.3b / v0.3c additive repairs
│   ├── models.py             # Issuer / Security / Filing / FinancialFact
│   ├── sec/                  # HTTP client + ticker / submissions / facts / PIT
│   ├── repositories/         # DuckDB persistence
│   ├── financials/           # canonical statements (on demand)
│   ├── factors/              # fiscal-period factors + ranking
│   ├── market/               # prices, shares, market cap, window analytics
│   └── portfolio/            # native research backtest (no skfolio yet)
├── tests/                    # offline pytest suite (fixtures + mocks)
├── tests/fixtures/sec/       # canned EDGAR JSON
├── docs/                     # architecture, data model, roadmap, this map
├── scripts/bootstrap.sh
├── .github/workflows/ci.yml
├── pyproject.toml
└── .env.example
```

There is no frontend, no skfolio adapter, and no second market provider.

## Runtime architecture

```text
ticker
  -> sec.tickers.resolve_ticker
  -> sec.submissions.fetch_all_submissions (+ archives)
  -> sec.company_facts.fetch_company_facts
  -> sec.normalization (Issuer / Security / Filing / FinancialFact, available_at)
  -> repositories persist to DuckDB
  -> financials.service.FinancialsService.get_snapshot(as_of)
  -> factors.engine / ranking
  -> CLI

market symbol
  -> market.service.MarketDataService.ingest
  -> market.providers.twelve_data.TwelveDataProvider
  -> validate / date-range / timezone / available_at
  -> repositories.market persist bars + run audit
  -> market.shares + market.market_cap (raw close × PIT shares)
  -> factors.engine (valuation: native market cap)
  -> market.analytics (adjusted closes; 12-1 / vol / beta / drawdown)

portfolio backtest
  -> portfolio.calendar (SPY reference sessions)
  -> financials.list_available_annual_periods + factors.engine (latest FY only)
  -> factors.ranking.rank_factor_results (per-name periods)
  -> portfolio.baselines (equal weight / inverse vol)
  -> portfolio.engine (drift, costs, equity)
  -> repositories.portfolio persist
  -> CLI `equitytrace portfolio backtest`
```

### SEC ingestion

Starts at `src/equitytrace/repositories/ingestion.py` (`IngestionRepository.ingest_ticker`)
and the CLI command `equitytrace ingest` in `src/equitytrace/cli.py`.

HTTP lives only in `src/equitytrace/sec/client.py` (`SecClient`). Endpoint helpers:

* `sec/tickers.py` — `company_tickers.json`
* `sec/submissions.py` — primary + archived submissions merge
* `sec/company_facts.py` — XBRL company facts
* `sec/normalization.py` — typed records and `available_at`

Archive filenames are sanitized in `sec/client.py` (`sanitize_archive_filename`)
before any URL or cache path is built.

### Canonical financial statements

`src/equitytrace/financials/service.py` (`FinancialsService.get_snapshot`).

* mappings: `financials/mappings.py`
* period parse: `financials/periods.py` (`FY2023`, `Q3-2023`)
* selection: `financials/selector.py`
* derived metrics: `financials/derived.py` (FCF, net debt, etc.; no invention)

Snapshots are computed on demand. They are not a materialized table.

### Factors and rankings

* registry: `src/equitytrace/factors/registry.py` (`_FACTORS`, aliases)
* implementations: sibling modules (`revenue_growth.py`, `valuation.py`, …)
* engine / persist: `factors/engine.py` → `repositories/factors.py`
  (engine owns `MarketCapService` for valuation factors)
* ranking: `factors/ranking.py` (`rank_factors` / `rank_factor_results`;
  `FactorEngine.rank` uses the same `calculate` path)

Valuation factors: `fcf_yield`, `price_to_earnings`, `price_to_sales`,
`price_to_book`. See `docs/metrics.md`.

### Market data

Orchestration: `src/equitytrace/market/service.py` (`MarketDataService.ingest`).

Twelve Data is isolated in `src/equitytrace/market/providers/twelve_data.py`.
The provider protocol is `market/providers/base.py`. Do not import Twelve Data
from financials or factors.

Window analytics: `src/equitytrace/market/analytics.py` (stored bars only,
`PriceAdjustmentMode.ALL`, bounded lookback). Beta intersects common price
dates before computing returns. `MarketAnalyticsService.rank` owns metric
ranking; beta ranking is refused.

CLI: `src/equitytrace/cli_market.py`
(`market ingest|prices|cap|cap-series|analytics|rank-metric`).

### Shares and market cap

* shares: `src/equitytrace/market/shares.py` (PIT SEC facts; never period-end as availability)
* market cap: `src/equitytrace/market/market_cap.py`
  (`raw close × shares`; refuse multi-class issuer ambiguity)
* bar `available_at`: `src/equitytrace/market/availability.py`
  (exchange-local 16:15 → UTC; unknown TZ rejected)

## File responsibility map

| Path | Responsibility |
| --- | --- |
| `src/equitytrace/cli.py` | SEC/statements/factors/rank CLI; `filingedge` deprecation wrapper |
| `src/equitytrace/cli_market.py` | Market CLI; analytics/rank-metric; non-zero on failed ingest |
| `src/equitytrace/cli_portfolio.py` | `equitytrace portfolio backtest` |
| `src/equitytrace/config.py` | pydantic-settings; `EQUITYTRACE_*` / legacy `FILINGEDGE_*` |
| `src/equitytrace/database.py` | DuckDB schema + additive v0.2 / v0.3a / v0.3b / v0.3c repairs |
| `src/equitytrace/models.py` | Core SEC domain models + `fact_id` |
| `src/equitytrace/sec/client.py` | HTTPX, rate limit, atomic cache, archive filename safety |
| `src/equitytrace/sec/tickers.py` | Ticker → CIK |
| `src/equitytrace/sec/submissions.py` | Submissions + archive merge |
| `src/equitytrace/sec/company_facts.py` | Company Facts fetch |
| `src/equitytrace/sec/normalization.py` | `available_at`, filings, facts |
| `src/equitytrace/repositories/ingestion.py` | Company ingest orchestration |
| `src/equitytrace/repositories/issuers.py` | `issuers` |
| `src/equitytrace/repositories/securities.py` | `securities` |
| `src/equitytrace/repositories/filings.py` | `filings` |
| `src/equitytrace/repositories/facts.py` | `financial_facts` + `get_facts_as_of` |
| `src/equitytrace/repositories/factors.py` | `factor_runs` / `factor_values` |
| `src/equitytrace/repositories/market.py` | Market tables, mappings, bars, run audit; batch bar reads |
| `src/equitytrace/repositories/portfolio.py` | `portfolio_runs` / rebalances / transitions / equity |
| `src/equitytrace/financials/` | Canonical statements + `list_available_annual_periods` |
| `src/equitytrace/factors/registry.py` | Factor name → implementation |
| `src/equitytrace/factors/ranking.py` | Deterministic cross-sectional rank |
| `src/equitytrace/factors/engine.py` | Calculate + native market cap + optional persist |
| `src/equitytrace/factors/valuation.py` | P/E, P/S, P/B |
| `src/equitytrace/market/service.py` | Ingest orchestration, bar validation, run finalize |
| `src/equitytrace/market/providers/twelve_data.py` | Only Twelve Data adapter |
| `src/equitytrace/market/shares.py` | PIT shares outstanding |
| `src/equitytrace/market/market_cap.py` | Historically safe market cap |
| `src/equitytrace/market/analytics.py` | 12-1 momentum, 1y vol/beta/drawdown from stored bars |
| `src/equitytrace/market/availability.py` | Bar availability convention |
| `src/equitytrace/portfolio/models.py` | Request/result/status types |
| `src/equitytrace/portfolio/calendar.py` | SPY (or other) reference sessions |
| `src/equitytrace/portfolio/signals.py` | Latest-FY factor eligibility + exact top-N |
| `src/equitytrace/portfolio/returns.py` | Common-date return matrices |
| `src/equitytrace/portfolio/baselines.py` | Equal weight / inverse vol targets only |
| `src/equitytrace/portfolio/engine.py` | Drift, costs, orchestration |
| `src/equitytrace/portfolio/metrics.py` | Compact performance statistics |
| `src/equitytrace/portfolio/audit.py` | Leakage audit result |
| `docs/metrics.md` | Valuation, market-metric, and backtest formulas |
| `docs/data-model.md` | Persistence entities, migrations, availability timestamps |
| `.github/workflows/ci.yml` | quality (ruff/format/mypy/pytest), package-smoke, hash-seed determinism |

## Persistence map

Schema source of truth: `src/equitytrace/database.py` (`SCHEMA_SQL`).

Initialization: `initialize_database()` → `Database.initialize()`.

Additive repairs (existing DBs):

* `_ensure_market_symbol_mapping_schema` (surrogate `mapping_id`)
* `_ensure_market_child_tables_without_fk` (DuckDB parent-UPDATE limitation)
* `_ensure_market_instrument_metadata_confirmed`
* `_ensure_factor_values_market_input`

Migration labels in `schema_migrations`: `0.2.0`, `0.3.0-a`, legacy `0.3.0a`,
`0.3.0-a1`, `0.3.0-a2`, `0.3.0-b`, `0.3.0-c`.

| Table | Written by | Notes |
| --- | --- | --- |
| `issuers` | `repositories/issuers.py` | CIK PK |
| `securities` | `repositories/securities.py` | `(ticker, cik)`; issuer ≠ security |
| `filings` | `repositories/filings.py` | `available_at` required |
| `financial_facts` | `repositories/facts.py` | deterministic `fact_id`; per-CIK DELETE+INSERT on ingest |
| `ingestion_runs` | `repositories/ingestion.py` | ingest audit |
| `schema_migrations` | `database.py` | logical versions |
| `factor_runs` / `factor_values` | `repositories/factors.py` | optional materialization; nullable `market_input_json` |
| `market_instruments` | `repositories/market.py` | `canonical_symbol` UNIQUE in v0.3a |
| `market_symbol_mappings` | `repositories/market.py` | `valid_from` / `valid_to`; inverted intervals rejected |
| `daily_price_bars` | `repositories/market.py` | unique `(instrument_id, provider, trading_date, adjustment_mode)` |
| `market_data_runs` | `repositories/market.py` | ingest provenance; must be finalized |
| `portfolio_runs` | `repositories/portfolio.py` | backtest config, warnings, audit |
| `portfolio_rebalances` | `repositories/portfolio.py` | PK `(run_id, decision_at)`; unique effective time |
| `portfolio_weight_transitions` | `repositories/portfolio.py` | no fills/shares/quantities |
| `portfolio_equity` | `repositories/portfolio.py` | session marks; PK `(run_id, valuation_at)` |

Caches (gitignored under `data/`):

* SEC: `EQUITYTRACE_CACHE_DIR` (default `data/cache`)
* Market: `EQUITYTRACE_MARKET_DATA_CACHE_DIR` (default `data/cache/market`)

No live cache, `.env`, or DuckDB file is committed.

## Point-in-time and financial correctness invariants

These must never be violated:

1. **`available_at <= as_of`** is the only safe knowledge filter. Period-end is
   not availability.
2. Date-only SEC acceptance (`2024-02-15`, `20240215`) is **not** midnight
   Eastern. Treat as unknown clock time and fall back to end-of-day Eastern on
   the filing date (`sec/normalization.py`).
3. Zoned SEC timestamps (`Z` / offsets) are absolute instants, not Eastern wall
   time.
4. Market bars outside the requested `[start_date, end_date]` must not persist.
5. Instrument currency/timezone is untrusted until a validated bar confirms it
   (`market_metadata_confirmed`). Unknown TZ cannot compute `available_at`.
6. Duplicate trading dates across Twelve Data windows: identical rows collapse;
   conflicting rows are dropped deterministically (sorted merge, not dict-hash order).
7. Multi-class issuers: never `issuer shares × one class price`. Return
   unavailable with explanation.
8. Market cap uses **raw** close only. Provider-adjusted history is not a
   vendor-vintage PIT archive.
9. Rankings must not depend on `PYTHONHASHSEED`. Sort by value then ticker.
10. Failed market ingest must not exit CLI `0`.
11. `market_data_run` finalization failures must be surfaced (no blanket
    `contextlib.suppress(Exception)`).
12. Ambiguous financial semantics → `UNAVAILABLE` + reason, never a guess.
13. Beta aligns common price dates before returns so both series cover identical intervals.
14. Market-window metrics use adjusted closes and must not claim vintage PIT.
15. Do not scan unbounded price history for a 1y metric (~550 calendar days).
16. Portfolio research returns use adjusted closes with
    `provider_adjusted_history_not_vintage_pit`. Valuation/market cap stays raw.
17. Decision evidence must have `available_at <= decision_at`. Target effective
    is the next reference-calendar session.
18. Weights drift between rebalances. Later unavailable rebalances charge no cost.
19. A held name missing an interval return fails the run (`held_return_missing`).
20. Exact top-N: valid count `< N` is unavailable; cutoff ties are not auto-included.

## Test ownership

| Subsystem | Tests |
| --- | --- |
| SEC client / cache / archives | `tests/test_sec_client.py` |
| Ticker resolution | `tests/test_ticker_resolution.py` |
| Normalization + acceptance time | `tests/test_normalization.py` |
| Point-in-time predicates | `tests/test_point_in_time.py` |
| Schema / init | `tests/test_database.py` |
| v0.2 additive migration | `tests/test_migration_v02.py` |
| Rename / env aliases | `tests/test_rename.py` |
| CLI (SEC / v0.2) | `tests/test_cli.py`, `tests/test_cli_v02.py` |
| Statements | `tests/test_financials.py` |
| Factors / ranking | `tests/test_factors.py` |
| Valuation factors | `tests/test_valuation_factors.py` |
| v0.3b adversarial correctness | `tests/test_v031_adversarial.py` |
| v0.2 audit | `tests/test_audit_v02.py` |
| v0.3b factor_values column | `tests/test_migration_v03b.py` |
| Market provider | `tests/test_market_provider.py` |
| Market core ingest | `tests/test_market_core.py` |
| Market CLI | `tests/test_market_cli.py` |
| Market audit / PIT / shares | `tests/test_market_audit.py` |
| Market analytics / PIT / ranking | `tests/test_market_analytics.py` |
| Market hardening | `tests/test_market_harden.py` |
| Market final edge cases | `tests/test_market_final.py` |
| v0.3a release-readiness regressions | `tests/test_release_audit.py` |
| Package version alignment | `tests/test_version_consistency.py` |
| Native portfolio backtest | `tests/test_portfolio_models.py`, `tests/test_portfolio_backtest.py` |
| Portfolio persistence | `tests/test_portfolio_repository.py` |
| Portfolio CLI | `tests/test_portfolio_cli.py` |
| v0.3c migration | `tests/test_migration_v03c.py` |
| Portfolio leakage / restatements | `tests/test_portfolio_leakage.py` |
| Fixtures | `tests/fixtures/sec/`, `tests/helpers/` |

All of these tests are offline. CI must not call live SEC or Twelve Data.

## CI ownership

`.github/workflows/ci.yml` is the only GitHub Actions workflow.

It checks:

* lockfile integrity (`uv lock --check`) then a locked install
* static quality: Ruff, format, Mypy, full pytest
* focused ranking determinism under `PYTHONHASHSEED` 0 / 1 / 42
* wheel + sdist build, artifact hygiene, clean install, installed CLI

Local packaging check: `uv run --no-project python scripts/package_smoke.py`.

## Current release-risk hotspots

v0.3b correctness concentrates here:

* `factors/engine.py` — native market-cap resolution vs manual override
* `factors/valuation.py` + `factors/free_cash_flow.py` — FY-only P/E/P/S, raw cap
* `market/analytics.py` — 253-bar windows, PIT `available_at`, no forward-fill
* `market/market_cap.py` — raw close, multi-class, staleness
* `database.py` — additive `market_input_json`
* `cli_market.py` — analytics / rank-metric; beta ranking refused

v0.3a correctness still concentrates here:

* `sec/normalization.py` — acceptance / `available_at`
* `sec/client.py` — cache replace + archive names
* `market/service.py` — date-range reject, metadata-after-validate, run finalize
* `market/providers/twelve_data.py` — window merge / conflicts
* `cli_market.py` — failed ingest exit code

## Roadmap touchpoints

See `docs/roadmap.md`. v0.3a is complete in v0.3.0. v0.3b is complete in
v0.3.1 (latest release). Native v0.3c portfolio/backtest foundation is
implemented on `0.3.2.dev0` and in review. The skfolio adapter is **not yet
implemented**. v0.3c is not complete until that adapter lands.

### v0.3a

Market-data foundation (**completed** in v0.3.0): provider-neutral daily OHLCV,
Twelve Data, raw vs adjusted rows, instruments/mappings, PIT shares, market cap,
market CLI.

### v0.3b

**Completed** in v0.3.1. Lives in existing `factors/` and `market/` packages
(no new top-level package):

* native FCF yield from stored market cap
* P/E, P/S, P/B
* momentum, volatility, beta, drawdown
* `docs/metrics.md`

### v0.3c

Native research portfolio/backtest (**implemented, in review**, package
`0.3.2.dev0`):

```text
src/equitytrace/portfolio/
├── models.py
├── calendar.py
├── signals.py
├── returns.py
├── baselines.py
├── engine.py
├── metrics.py
└── audit.py
```

CLI: `equitytrace portfolio backtest`. Persistence: `0.3.0-c` tables.

**skfolio adapter = NOT YET IMPLEMENTED.** Do not add `optimizer.py`,
`skfolio_adapter.py`, or `constraints.py` in this slice.

### v1.0

Strategy builder and research interface (not in this repo layout today).

### post-v1 / separate quant platform

Live trading, brokers, NautilusTrader, Kronos, crypto, Hummingbot, Freqtrade
are out of scope for EquityTrace itself.

## Where to start for common tasks

| Task | Start here |
| --- | --- |
| SEC ingest bug | `repositories/ingestion.py` → `sec/client.py` → `sec/normalization.py` |
| `available_at` wrong | `sec/normalization.py` (`parse_acceptance_datetime`, `resolve_available_at`) |
| Fact/filing persistence | `repositories/facts.py`, `repositories/filings.py` |
| Statement mapping miss | `financials/mappings.py` + `financials/selector.py` |
| Factor formula / registry | `factors/<name>.py` + `factors/registry.py` |
| Ranking nondeterminism | `factors/ranking.py` / `market/analytics.py` |
| Valuation / native cap | `factors/engine.py`, `factors/valuation.py` |
| Market ingest | `market/service.py` |
| Twelve Data payload | `market/providers/twelve_data.py` only |
| Shares outstanding | `market/shares.py` |
| Market cap / multi-class | `market/market_cap.py` |
| Momentum / vol / beta / DD | `market/analytics.py` |
| Portfolio backtest | `portfolio/engine.py` + `cli_portfolio.py` |
| Schema / migration | `database.py` + `tests/test_migration_v02.py` / `tests/test_migration_v03b.py` / `tests/test_migration_v03c.py` |
| CLI exit codes | `cli.py` / `cli_market.py` / `cli_portfolio.py` |
| Env vars | `config.py` + `.env.example` |

## How to use this map

1. Read **this map**.
2. Read at most the bounded files in the matching row above.
3. Read the owning test file before changing behavior.
4. Search more broadly only if the bounded files do not contain the answer.
5. Do not scan `tests/fixtures/` or the whole `src/` tree by default.
6. Do not add a skfolio optimizer from this map.

## Verification commands

```bash
uv sync --all-groups --no-editable --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Targeted v0.3a / PIT:

```bash
uv run pytest \
  tests/test_sec_client.py \
  tests/test_normalization.py \
  tests/test_point_in_time.py \
  tests/test_database.py \
  tests/test_migration_v03b.py \
  tests/test_valuation_factors.py \
  tests/test_v031_adversarial.py \
  tests/test_market_analytics.py \
  tests/test_market_provider.py \
  tests/test_market_core.py \
  tests/test_market_cli.py \
  tests/test_market_audit.py \
  tests/test_market_harden.py \
  tests/test_market_final.py \
  tests/test_release_audit.py
```

Determinism (full suite):

```bash
PYTHONHASHSEED=0 uv run pytest
PYTHONHASHSEED=1 uv run pytest
PYTHONHASHSEED=42 uv run pytest
```

## Map maintenance checklist

Update this file in the same PR when any of the following change:

- [ ] Module moved, renamed, or given a new responsibility
- [ ] New persistence table, index, or migration helper
- [ ] `available_at` / PIT / market-cap invariant changed
- [ ] Test file ownership changed
- [ ] A planned package (portfolio, UI, extra provider) is actually created
- [ ] CLI commands or env vars added/removed
- [ ] Release track (v0.3a / v0.3b / v0.3c) boundary moved
