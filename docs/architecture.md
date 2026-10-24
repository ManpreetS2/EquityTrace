# Architecture (1.0.0; latest published release v0.3.2)

EquityTrace is a layered Python application with a clear boundary between SEC I/O,
normalization, persistence, canonical statements, factors, market data, and CLI.

Domain objects stay separate:

```text
Issuer
Security
Filing
FinancialFact
Financial statements
Fundamental factors
Market data
Market analytics
Portfolio/backtest — native weight-return research (v0.3c)
Research strategies — typed JSON + research CLI (v1.0 Milestone 1)
Presentation/HTML report — future (v1.0 Milestone 2)
```

## Layers

1. **CLI (`equitytrace.cli`)**
   Typer commands with Rich output. Converts user errors into readable messages
   (no stack traces for expected failures).

2. **Configuration (`equitytrace.config`)**
   Environment-driven settings via pydantic-settings (`EQUITYTRACE_*`, with
   temporary `FILINGEDGE_*` fallbacks). Builds the SEC User-Agent from
   organization + contact email + application identifier.

3. **SEC client (`equitytrace.sec.client`)**
   HTTPX client with identifying User-Agent, rate limiting, retries, optional
   disk cache with a mutable-endpoint TTL (default 24h) plus immutable archive
   policy, `force_refresh` / `ingest --refresh`, and injectable transport for
   offline tests.

4. **Endpoint helpers**
   - `tickers.py` — company_tickers.json resolution
   - `submissions.py` — primary + archived submissions merge
   - `company_facts.py` — XBRL company facts fetch
   - `normalization.py` — typed domain records + `available_at`

5. **Domain models (`equitytrace.models`)**
   Pydantic models: `Issuer`, `Security`, `Filing`, `FinancialFact`, run summaries.

6. **Persistence**
   - `database.py` — DuckDB path + idempotent / additive schema
   - repositories for issuers, securities, filings, facts, ingestion, factors, portfolio
   - company snapshot writes with idempotent re-ingest semantics

7. **Canonical financials (`equitytrace.financials`)**
   - mapping registry (`mappings.py`) from canonical concepts → XBRL tags
   - period parsing (`FY2023`, `Q3-2023`)
   - deterministic concept selection with provenance
   - YTD → standalone quarter derivation
   - on-demand `FinancialsService.get_snapshot(...)`

8. **Factors (`equitytrace.factors`)**
   - protocol-style factor classes
   - point-in-time-safe calculations from snapshots
   - valuation multiples use stored PIT market cap (raw close × shares)
   - cross-sectional ranking with explicit direction
   - optional materialization into `factor_runs` / `factor_values`

## Point-in-time design

```text
acceptanceDateTime with Z / explicit offset  -> absolute instant (UTC) => available_at
naive acceptanceDateTime                     -> U.S. Eastern wall time -> UTC => available_at
missing or date-only acceptance              -> filing-date end-of-day Eastern -> UTC => available_at
```

Queries and snapshots:

```python
FactsRepository(...).get_facts_as_of(ticker="AAPL", as_of=dt)
FinancialsService(...).get_snapshot("AAPL", "FY2023", as_of=dt)
# predicate: available_at <= as_of
```

Report period `end` dates are never used as availability.

## Canonical statement design

Canonical statements are **calculated on demand** from stored XBRL facts.
This keeps historical `as_of` queries correct without storing every possible
snapshot. Factor outputs may optionally be materialized for audit/idempotency.

Concept selection prefers:

1. facts available at `as_of`
2. exact fiscal period match (`fy` / `fp`)
3. form type (10-K annual / 10-Q quarterly)
4. duration length consistent with a single year (~365d) or quarter (~90d)
5. latest period end date (10-K comparative years often reuse the current `fy`/`fp`)
6. latest accepted filing available at `as_of` (amendments included only then)
7. higher-priority mapping candidates
8. deterministic conflict resolution with warnings when equal-score values disagree

Sign normalization (for example CapEx `Payments*` tags) is declared on each
mapping. The raw SEC value is retained in `provenance.reported_value`; the
canonical value is the normalized magnitude used by formulas such as
`FCF = OCF - CapEx`.

Standalone-quarter derivation:

```text
Q2 = six-month YTD − Q1
Q3 = nine-month YTD − six-month YTD
```

Derivation requires aligned period starts (≤ 7 day tolerance) and matching
units. Q1 is never derived. Q4 is not casually derived from annual − YTD.

Quality score missing inputs are excluded from the denominator (no credit for
unavailable components). Accrual ratio ranks **lower_is_better** (cash closer
to earnings scores better). Ranking ties share competition rank and percentile.

## Offline testing

`httpx.MockTransport` serves fixtures from `tests/fixtures/sec/`. Statement and
factor tests use fictional seeded facts. Market-provider tests use mock
transports or injected providers. No live SEC or Twelve Data calls in CI.


9. **Market data (`equitytrace.market`)**
   - Provider protocol (`MarketDataProvider`) with Twelve Data as the first implementation
   - Date-range requests omit `outputsize` (avoids silent truncation) and use overlapping calendar windows
   - Requests-per-minute limiter (monotonic clock) on live calls only; cache hits do not consume quota
   - Raw and adjusted daily bars stored as distinct rows
   - Conservative `available_at` = exchange-local 16:15 → UTC (unknown timezones rejected)
   - Shares outstanding selected from stored SEC facts with point-in-time filters
   - Market cap = raw close × split-reconciled shares when verified split coverage
     exists for `(shares_fact_date, price_date]`; otherwise unavailable
   - Explicit `market ingest-splits` uses Twelve Data `GET /splits` (paid plan);
     never inferred from adjusted/raw price ratios; multi-class issuers unavailable

10. **Market analytics (`equitytrace.market.analytics`)**
   - Window metrics from stored adjusted bars, not fiscal-period factors
   - 12-1 momentum, 1y volatility, beta, and max drawdown
   - Beta aligns common price dates before returns
   - Ranking for metrics with a default direction; beta ranking is refused

11. **Portfolio backtest (`equitytrace.portfolio`)**
   - Weight/notional research model (no fills, shares, or execution)
   - Decision after session `available_at`; target effective next calendar session
   - Latest FY per security/ticker (no silent older-year fallback)
   - Exact top-N; equal weight, inverse vol, or skfolio-backed minimum variance
     on common-date 252-return matrices (long-only, fully invested, CLARABEL;
     no optimizer fallback)
   - Drift, gross turnover, symmetric bps costs, leakage audit (costs are never
     passed into skfolio)
   - Native v0.3c backtests require `adjustment_mode=all` (provider-adjusted
     research closes). Raw close remains valuation / market-cap only.
   - Every research-universe ticker must resolve an issuer CIK
     (`issuer_identity_unavailable` otherwise). Calendar and benchmark symbols
     are not subject to that rule unless they are also universe tickers.
   - Multiple universe securities that share one issuer CIK are unavailable
     (`multiple_securities_same_issuer_unsupported`); v0.3c does not choose a
     share class.
   - Explicit schedules fail closed: every unique requested date must form an
     in-window (decision, target-effective) pair.
   - `portfolio.skfolio_adapter` is a narrow ReturnMatrix → target-weight bridge
     only (no WalkForward, turnover-aware optimization, or alternate objectives)

v1.0 Milestone 1 adds `strategy/` (ResearchStrategy → BacktestRequest + economic
fingerprint) and `equitytrace research validate|run|show` on top of the existing
BacktestEngine. The same normalized BacktestRequest, EquityTrace package version,
and DuckDB evidence must produce economically equivalent status, failure reason,
metrics, equity path, rebalance path, target weights, warnings, and audit.
`run_id`, `created_at`, and `completed_at` are execution identifiers, not
economic identity. Strategy name and description stay in the strategy file;
`research show` recomputes the fingerprint from persisted `request_json` and
does not promise the display name.

v1.0 Milestone 2 adds `reporting/` and `equitytrace research report|export`.
Reports and exports read persisted runs via `PortfolioRepository.get_run` in a
read-only DuckDB session. They do not rerun the engine, ingest SEC/market data,
mutate stored research, or introduce a web framework. HTML is a self-contained
offline document with inline SVG charts; JSON exports (`request.json`,
`result.json`) are deterministic and include the economic fingerprint in export
metadata. Report-generation success is distinct from research status
(SUCCESS / UNAVAILABLE / FAILED). There is no schema change for this milestone.
v1.0 Milestone 3 productizes the completed CLI + offline HTML workflow, ships an
offline synthetic demonstration (`scripts/demo_research.py`), and prepares
package version `1.0.0`. Publishing the annotated `v1.0.0` tag / GitHub Release
is a separate approval step. Additional optimizers remain out of scope.
