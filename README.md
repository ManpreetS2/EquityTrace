# EquityTrace

**Transparent stock research backed by filings, factors, and historical evidence.**

EquityTrace is a Python-first quantitative research platform that transforms
official SEC filings into point-in-time financial statements, transparent
fundamental factors, and reproducible company rankings.

Every normalized value and factor can be traced to its underlying SEC concept,
filing accession, reporting period, and public availability timestamp.

Version **v0.2** builds on the v0.1 EDGAR ingestion pipeline (historically
published as FilingEdge): resolve tickers to CIKs, retrieve submissions and XBRL
Company Facts, normalize them into structured records, store them in DuckDB,
assemble comparable financial statements, and calculate point-in-time-safe
factors.

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

Acceptance timestamps are interpreted in U.S. Eastern time and stored as
timezone-aware UTC. If acceptance time is missing, EquityTrace uses a
**conservative** fallback: end of the filing date in Eastern time (not the start
of that day).

## Architecture overview

```text
Ticker -> SEC CIK
      -> submissions (+ archives)
      -> company facts (XBRL)
      -> normalize (Issuer / Security / Filing / FinancialFact)
      -> DuckDB (atomic upsert)
      -> canonical statement snapshot (as_of)
      -> fundamental factors / ranking
      -> CLI
```

Key separations:

- **Issuer** (CIK / legal entity) ≠ **Security** (ticker / share class)
- Report period dates ≠ public availability dates
- Raw XBRL concepts ≠ canonical statement concepts
- Live SEC HTTP client is injectable; tests use offline fixtures

See [docs/architecture.md](docs/architecture.md) and [docs/data-model.md](docs/data-model.md).

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
| `fcf_yield` | FCF / market cap (market cap must be supplied) | higher better |
| `debt_change` | YoY total-debt change | lower better |
| `roa` | NI / average total assets | higher better |
| `accrual_ratio` | (NI − OCF) / average assets | lower better |
| `basic_quality_score` | Transparent weighted good/bad composite | higher better |

Accrual ratio definition: earnings accruals relative to average assets. Lower
values mean cash flow closer to net income.

FCF yield returns an unavailable result when market capitalization is not
explicitly supplied. Market prices belong to v0.3.

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
| `EQUITYTRACE_HTTP_TIMEOUT_SECONDS` | Request timeout |
| `EQUITYTRACE_MAX_REQUESTS_PER_SECOND` | Rate limit (< 10) |

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
uv run equitytrace factors AAPL --period FY2023
uv run equitytrace rank --tickers AAPL,MSFT,GOOGL --factor revenue-growth --period FY2023

uv run equitytrace db-info
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

All SEC tests run offline against fixtures under `tests/fixtures/sec/` or
fictional seeded facts.

## Current limitations (v0.2)

- No historical market prices (FCF yield needs an explicit `--market-cap`)
- No portfolios, rebalancing, or backtests
- No frontend / research UI
- Canonical statements are computed on demand (not fully materialized)
- Concept mappings cover common us-gaap tags; unusual issuer tags may be missing
- 10-K comparative columns that reuse the current `fy`/`fp` are disambiguated by
  period end date and duration heuristics; unusual fiscal calendars may still warn
- Q4 is not casually derived from annual − nine-month YTD
- Large live Company Facts ingestions use per-CIK DELETE+INSERT without a
  multi-statement DuckDB transaction (re-ingest is idempotent; mid-failure can
  leave issuer rows that a successful re-ingest repairs)
- Company Facts availability uses filing acceptance when the accession is present
  in stored filings; otherwise falls back to the fact’s `filed` date (end of day Eastern)
- HTML filing scraping is intentionally out of scope

## Roadmap

See [docs/roadmap.md](docs/roadmap.md).

- **v0.3** — Prices, portfolios, transaction-cost-aware backtests
- **v1.0** — Strategy builder and research interface

## License

MIT
