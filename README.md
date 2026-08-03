# FilingEdge

FilingEdge is a Python-first quantitative finance foundation for **point-in-time SEC filing ingestion** and fundamental analysis.

Version **v0.1** focuses on a production-quality EDGAR ingestion pipeline: resolve tickers to CIKs, retrieve submissions and XBRL Company Facts, normalize them into structured records, store them in DuckDB, and query facts as they were known on a historical date.

FilingEdge does **not** predict stock prices.

## Why look-ahead bias matters

A backtest that uses a fiscal-period end date as the date a number became known will silently cheat.

Example:

- Q1 revenue *period* ended 2023-12-30
- The 10-Q was *accepted by the SEC* on 2024-02-15 16:30:15 Eastern

A strategy deciding trades on 2024-01-31 must not see that revenue figure. FilingEdge stores an `available_at` timestamp for every filing and fact and filters with:

```text
available_at <= as_of
```

Acceptance timestamps are interpreted in U.S. Eastern time and stored as timezone-aware UTC. If acceptance time is missing, FilingEdge uses a **conservative** fallback: end of the filing date in Eastern time (not the start of that day).

## Architecture overview

```text
Ticker -> SEC CIK
      -> submissions (+ archives)
      -> company facts (XBRL)
      -> normalize (Issuer / Security / Filing / FinancialFact)
      -> DuckDB (atomic upsert)
      -> point-in-time queries / CLI
```

Key separations:

- **Issuer** (CIK / legal entity) ≠ **Security** (ticker / share class)
- Report period dates ≠ public availability dates
- Live SEC HTTP client is injectable; tests use offline fixtures

See [docs/architecture.md](docs/architecture.md) and [docs/data-model.md](docs/data-model.md).

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
# edit .env and set FILINGEDGE_SEC_EMAIL to a real contact email

uv sync --all-groups --no-editable
uv run filingedge init-db
```

On macOS, prefer `--no-editable`. Editable installs can produce `.pth` files
marked `UF_HIDDEN`, which Python 3.12+ ignores, breaking `uv run filingedge`.

Or use the bootstrap script:

```bash
./scripts/bootstrap.sh
```

## Environment variables

| Variable | Purpose |
| --- | --- |
| `FILINGEDGE_SEC_EMAIL` | **Required** for live SEC requests (User-Agent contact) |
| `FILINGEDGE_SEC_ORGANIZATION` | Organization name in User-Agent (default: `FilingEdge`) |
| `FILINGEDGE_DATABASE_PATH` | DuckDB path (default: `data/filingedge.duckdb`) |
| `FILINGEDGE_CACHE_DIR` | Optional HTTP cache directory |
| `FILINGEDGE_HTTP_TIMEOUT_SECONDS` | Request timeout |
| `FILINGEDGE_MAX_REQUESTS_PER_SECOND` | Rate limit (< 10) |

No personal emails or secrets are committed. Tests do not require a real SEC email or network access.

## CLI examples

```bash
uv run filingedge resolve AAPL
uv run filingedge ingest AAPL
uv run filingedge filings AAPL --form 10-K --limit 20
uv run filingedge facts AAPL --concept Revenue --limit 20
uv run filingedge facts-as-of AAPL --date 2024-01-15
uv run filingedge db-info
```

## Database model

Tables:

- `issuers`
- `securities`
- `filings`
- `financial_facts`
- `ingestion_runs`

Facts use a deterministic `fact_id` so repeated ingestion is idempotent. Ingestion of normalized company data is transactional.

## Testing / quality

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

All SEC tests run offline against fixtures under `tests/fixtures/sec/`.

## Current limitations (v0.1)

- No canonical chart-of-accounts mapping across companies
- No derived factors (growth, margins, FCF yield, etc.)
- No market prices, portfolios, or backtests
- No frontend / research UI
- Company Facts availability uses filing acceptance when the accession is present in stored filings; otherwise falls back to the fact’s `filed` date (end of day Eastern)
- HTML filing scraping is intentionally out of scope

## Roadmap

See [docs/roadmap.md](docs/roadmap.md).

- **v0.2** — Canonical statements and factor library
- **v0.3** — Prices, portfolios, transaction-cost-aware backtests
- **v1.0** — Strategy builder and research interface

## License

MIT
