# EquityTrace

**Transparent stock research backed by filings, factors, and historical evidence.**

EquityTrace is a Python-first quantitative research platform that transforms
official SEC filings into point-in-time financial statements, transparent
fundamental factors, and reproducible company rankings.

Every normalized value and factor can be traced to its underlying SEC concept,
filing accession, reporting period, and public availability timestamp.

Version **0.3.1** is the latest release (v0.3b). The `0.3.2.dev0` line adds a
native **weight-return research backtester**: latest-FY factor selection per
security/ticker, exact top-N, equal-weight / inverse-vol / minimum-variance
baselines (minimum variance uses a narrow skfolio adapter on 252 common daily
returns), drifting weights, and transparent costs. It is not an execution
simulator and does not
invent fill prices or share quantities. v0.3c does not choose among multiple
securities/share classes for one issuer.

See [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) for module navigation.

EquityTrace does **not** predict stock prices and does **not** provide
personalized investment advice.

## Research and Beta Disclaimer

EquityTrace is an experimental research and educational tool.

* It does not provide personalized financial, legal, or tax advice.
* Factors and rankings are not instructions to buy, sell, or hold securities.
* Financial data may be incomplete, delayed, restated, or interpreted incorrectly.
* Historical performance will not guarantee future results.
* Investing involves risk, including loss of principal.
* Users must independently verify information before making any decision.

This disclaimer describes intended use; it is not a guarantee of legal outcomes.

## Why look-ahead bias matters

A backtest that uses a fiscal-period end date as the date a number became known
will silently cheat.

Example:

- Q1 revenue *period* ended 2023-12-30
- The 10-Q was *accepted by the SEC* on 2024-02-15 16:30:15 Eastern

A strategy deciding trades on 2024-01-31 must not see that revenue figure.
EquityTrace stores an `available_at` timestamp for every filing and fact and
filters with:

```text
available_at <= as_of
```

Acceptance timestamps with an explicit zone (including SEC ``Z`` / offset forms)
are treated as absolute instants and stored as timezone-aware UTC. Naive
timestamps without a zone are interpreted as U.S. Eastern wall time. If
acceptance time is missing or only a date is provided, EquityTrace uses a
**conservative** fallback: end of the filing date in Eastern time (not the start
of that day).

## Architecture overview

```text
Ticker -> SEC CIK
      -> submissions (+ archives)
      -> company facts (XBRL)
      -> normalize (Issuer / Security / Filing / FinancialFact)
      -> DuckDB persistence
      -> canonical statement snapshot (as_of)
      -> fundamental factors / ranking
      -> market data (raw + adjusted bars, PIT market cap)
      -> market-window analytics (momentum / vol / beta / drawdown)
      -> native research backtest (weight-return)
      -> CLI
```

Key separations:

- **Issuer** (CIK / legal entity) ≠ **Security** (ticker / share class)
- Report period dates ≠ public availability dates
- Raw XBRL concepts ≠ canonical statement concepts
- Fiscal-period factors ≠ market-window analytics
- Raw close (valuation / market cap) ≠ provider-adjusted close (momentum / risk)
- Live SEC HTTP client is injectable; tests use offline fixtures

See [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) for module navigation,
[docs/architecture.md](docs/architecture.md), and [docs/data-model.md](docs/data-model.md).

## Canonical statements

`FinancialsService.get_snapshot(ticker, period, as_of=...)` maps XBRL tags to
canonical income, balance-sheet, and cash-flow concepts using a dedicated
mapping registry (`equitytrace.financials.mappings`).

Selection rules prefer exact fiscal periods, form type (10-K / 10-Q), latest
accepted filing available at `as_of`, and higher-priority mappings. Conflicts
are resolved deterministically and surfaced as warnings with provenance.

Quarterly cumulative (YTD) cash-flow / income facts can be converted to
standalone quarters when period boundaries align:

```text
Q2 standalone = six-month YTD − Q1
Q3 standalone = nine-month YTD − six-month YTD
```

Derived values (`free_cash_flow`, `net_debt`, `working_capital`,
`invested_capital`, `ebitda`, `nopat`) are calculated only when required inputs
exist — EquityTrace does not invent missing facts.

## Fundamental factors

| Factor | Definition | Ranking |
| --- | --- | --- |
| `revenue_growth` | YoY revenue growth | higher better |
| `operating_margin_change` | YoY operating-margin change | higher better |
| `free_cash_flow` | OCF − CapEx | higher better |
| `fcf_yield` | FCF / PIT market cap (optional `--market-cap` override) | higher better |
| `price_to_earnings` | FY market cap / net income (quarterly unavailable) | lower better |
| `price_to_sales` | FY market cap / revenue (quarterly unavailable) | lower better |
| `price_to_book` | market cap / stockholders' equity | lower better |
| `debt_change` | YoY total-debt change | lower better |
| `roa` | NI / average total assets | higher better |
| `accrual_ratio` | (NI − OCF) / average assets | lower better |
| `basic_quality_score` | Transparent weighted good/bad composite | higher better |

Accrual ratio definition: earnings accruals relative to average assets. Lower
values mean cash flow closer to net income.

FCF yield, P/E, P/S, and P/B use stored PIT market cap by default. Formulas and
unavailable conditions are in [docs/metrics.md](docs/metrics.md).

## Technology stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12+ |
| Packaging | `uv` |
| Dataframes | Polars |
| Storage | DuckDB |
| CLI | Typer + Rich |
| HTTP | HTTPX |
| Models | Pydantic |
| Quality | Pytest, Ruff, Mypy |

Approximate mix: **Python ~85%**, **SQL ~15%**, TypeScript **0%**.

## Setup

```bash
cp .env.example .env
# edit .env and set EQUITYTRACE_SEC_EMAIL to a real contact email

uv sync --all-groups --no-editable
uv run equitytrace init-db
```

On macOS, prefer `--no-editable`. Editable installs can produce `.pth` files
marked `UF_HIDDEN`, which Python 3.12+ ignores, breaking `uv run equitytrace`.

Or use the bootstrap script:

```bash
./scripts/bootstrap.sh
```

## Environment variables

Preferred names:

| Variable | Purpose |
| --- | --- |
| `EQUITYTRACE_SEC_EMAIL` | **Required** for live SEC requests (User-Agent contact) |
| `EQUITYTRACE_SEC_ORGANIZATION` | Organization name in User-Agent (default: `EquityTrace Development`) |
| `EQUITYTRACE_DATABASE_PATH` | DuckDB path (default: `data/equitytrace.duckdb`) |
| `EQUITYTRACE_CACHE_DIR` | Optional HTTP cache directory |
| `EQUITYTRACE_ENABLE_CACHE` | Enable SEC/market response caching (default: true) |
| `EQUITYTRACE_HTTP_TIMEOUT_SECONDS` | Request timeout |
| `EQUITYTRACE_MAX_REQUESTS_PER_SECOND` | Rate limit (< 10) |
| `EQUITYTRACE_MARKET_DATA_PROVIDER` | Market provider id (default: `twelve_data`) |
| `EQUITYTRACE_TWELVE_DATA_API_KEY` | Required for live `market` commands only |
| `EQUITYTRACE_MARKET_DATA_TIMEOUT_SECONDS` | Market HTTP timeout |
| `EQUITYTRACE_MARKET_DATA_REQUESTS_PER_MINUTE` | Market rate limit |
| `EQUITYTRACE_MARKET_DATA_MAX_RETRIES` | Transient retry budget |
| `EQUITYTRACE_MARKET_DATA_CACHE_DIR` | Local market response cache directory (gitignored; default: `data/cache/market`) |

Legacy `FILINGEDGE_*` variables are still accepted temporarily when the matching
`EQUITYTRACE_*` variable is unset. Prefer the new names; a deprecation warning
is emitted (without printing secret values). New variables override old ones.

Existing local databases (for example `data/filingedge.duckdb`) are not renamed
automatically. Point at them explicitly:

```env
EQUITYTRACE_DATABASE_PATH=data/filingedge.duckdb
```

No personal emails or secrets are committed. Tests do not require a real SEC
email or network access.

## CLI examples

```bash
uv run equitytrace resolve AAPL
uv run equitytrace ingest AAPL
uv run equitytrace filings AAPL --form 10-K --limit 20
uv run equitytrace facts AAPL --concept Revenue --limit 20
uv run equitytrace facts-as-of AAPL --date 2024-01-15

uv run equitytrace statements AAPL --period FY2023
uv run equitytrace statements AAPL --period Q3-2023 --as-of 2024-02-01
uv run equitytrace factor AAPL revenue-growth --period FY2023
uv run equitytrace factor AAPL fcf-yield --period FY2023 --as-of 2024-02-01
uv run equitytrace factors AAPL --period FY2023
uv run equitytrace rank --tickers AAPL,MSFT,GOOGL --factor price-to-book --period FY2023

uv run equitytrace db-info
uv run equitytrace market ingest AAPL --start 2020-01-01 --end 2024-12-31
uv run equitytrace market analytics AAPL --as-of 2024-12-31 --benchmark SPY
uv run equitytrace market rank-metric momentum_12_1 AAPL MSFT NVDA --as-of 2024-12-31
```

The deprecated `filingedge` console script still invokes the same CLI and prints
a deprecation warning. Prefer `equitytrace`.

## Database model

Tables:

- `issuers`
- `securities`
- `filings`
- `financial_facts`
- `ingestion_runs`
- `schema_migrations`
- `factor_runs`
- `factor_values`
- `market_instruments`
- `market_symbol_mappings`
- `daily_price_bars`
- `market_data_runs`

Existing v0.1 databases migrate by re-running `equitytrace init-db` (additive
`CREATE IF NOT EXISTS`). Facts use a deterministic `fact_id` so repeated
ingestion is idempotent. Internal table names do not depend on the product name.

## Testing / quality

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

The offline test suite runs entirely against fixtures and mocks; no live SEC or
Twelve Data access is required.

## Market data and analytics

EquityTrace stores **raw** (`adjustment_mode=none`) and **provider-adjusted**
(`adjustment_mode=all`) daily OHLCV bars as separate rows. Market capitalization
always uses **raw close × point-in-time SEC shares outstanding**. Valuation
multiples use that PIT cap. Momentum, volatility, beta, and drawdown use
adjusted closes and always warn `provider_adjusted_history_not_vintage_pit`.

Daily bars become available at **16:15 exchange-local time** (converted to UTC).
That is a conservative EquityTrace convention, not a claim about the exact
vendor publication instant.

Provider-adjusted history may be revised when corporate-action data changes; it
is **not** a true vendor-vintage point-in-time archive. Do not use adjusted
closes for market cap.

Share-class ambiguity (for example Alphabet) returns **unavailable** rather than
a fabricated class-level market cap.

Configure Twelve Data with `EQUITYTRACE_TWELVE_DATA_API_KEY`. Review the
provider’s licensing before redistribution, display, or commercial use. Live
responses and caches stay under ignored `data/` paths and are never committed.

```bash
uv run equitytrace market ingest AAPL --start 2020-01-01 --end 2024-12-31
uv run equitytrace market ingest SPY --asset-type etf --start 2020-01-01 --end 2024-12-31
uv run equitytrace market prices AAPL --start 2023-01-01 --end 2023-12-31 --adjustment none
uv run equitytrace market cap AAPL --date 2023-09-29
uv run equitytrace market cap-series AAPL --start 2022-01-01 --end 2023-12-31 --frequency month-end
uv run equitytrace market analytics AAPL --as-of 2024-12-31 --benchmark SPY
uv run equitytrace market rank-metric volatility_1y AAPL MSFT --as-of 2024-12-31
```

SEC-only commands continue to work without a market-data API key.

## Current limitations (v0.3.1)

- P/E and P/S are FY only; quarterly TTM is not assembled
- Provider-adjusted history is not a vendor-vintage PIT archive
- Beta has no default ranking direction
- Native backtests are research weight-return paths, not execution simulations
- v0.3c native backtests currently require `adjustment_mode=all`; raw close remains valuation-only
- v0.3c does not yet choose among multiple securities/share classes for one issuer; ambiguous same-issuer universes are unavailable
- Minimum variance only (no HRP, Black-Litterman, CVaR, or turnover-aware optimization)
- No frontend / research UI
- Canonical statements are computed on demand (not fully materialized)
- Concept mappings cover common us-gaap tags; unusual issuer tags may be missing
- 10-K comparative columns that reuse the current `fy`/`fp` are disambiguated by
  period end date, but unusual issuer tagging can still require mapping updates
- `canonical_symbol` is unique per instrument in v0.3a (historical ticker reuse
  across issuers is not fully modeled yet)
- Multi-class detection uses distinct tickers linked to one issuer CIK; the
  securities table has no share-class/active flag, so detection is conservative
- Optional market response cache has no TTL; disable cache to force fresh fetches
- Live Twelve Data responses are not exercised in CI (offline fixtures only)
- Period selection uses period-end date and duration heuristics; unusual fiscal
  calendars may still warn
- Q4 is not casually derived from annual − nine-month YTD
- Large live Company Facts ingestions use per-CIK DELETE+INSERT without a
  multi-statement DuckDB transaction (re-ingest is idempotent; mid-failure can
  leave issuer rows that a successful re-ingest repairs)
- Company Facts availability uses filing acceptance when the accession is present
  in stored filings; otherwise falls back to the fact’s `filed` date (end of day Eastern)
- HTML filing scraping is intentionally out of scope

## Roadmap

See [docs/roadmap.md](docs/roadmap.md).

- **v0.3.0 / v0.3a** — Market data foundation (**completed**)
- **v0.3.1 / v0.3b** — Valuation, momentum, and risk (**completed**)
- **v0.3c** — Native portfolio/backtest + min-variance adapter (**in review**)
- **v1.0** — Strategy builder and research interface

## License

MIT
