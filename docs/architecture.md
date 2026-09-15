# Architecture (v0.3.2.dev0)

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
Presentation/UI — future (v1.0)
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
   disk cache, and injectable transport for offline tests.

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
   - Market cap = raw close × shares; multi-class issuers are unavailable by default

10. **Market analytics (`equitytrace.market.analytics`)**
   - Window metrics from stored adjusted bars, not fiscal-period factors
   - 12-1 momentum, 1y volatility, beta, and max drawdown
   - Beta aligns common price dates before returns
   - Ranking for metrics with a default direction; beta ranking is refused

11. **Portfolio backtest (`equitytrace.portfolio`)**
   - Weight/notional research model (no fills, shares, or execution)
   - Decision after session `available_at`; target effective next calendar session
   - Latest FY per issuer (no silent older-year fallback)
   - Exact top-N, equal weight, inverse vol on common-date 252-return matrices
   - Drift, gross turnover, symmetric bps costs, leakage audit
   - skfolio adapter is not implemented yet

Future (not in this tree): skfolio min-variance (remainder of v0.3c) and a
research UI (v1.0).
