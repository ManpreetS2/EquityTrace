# Start here

Exact commands for a new developer on EquityTrace `0.3.2.dev0` (native v0.3c
portfolio/backtest foundation in review; v0.3.1 remains the latest release).

Read [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) first. Then skim
[docs/architecture.md](docs/architecture.md) and [docs/metrics.md](docs/metrics.md)
before searching the tree.

## 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set a real contact email:

```text
EQUITYTRACE_SEC_EMAIL=you@example.com
EQUITYTRACE_SEC_ORGANIZATION=EquityTrace Development
```

The SEC requires an identifying User-Agent for EDGAR fair access. Tests do **not** need this email.

Legacy `FILINGEDGE_*` variables still work temporarily when the matching
`EQUITYTRACE_*` variable is unset.

To keep using an older local database path without moving files:

```text
EQUITYTRACE_DATABASE_PATH=data/filingedge.duckdb
```

## 2. Install dependencies

Requires [uv](https://github.com/astral-sh/uv) and Python 3.12+.

```bash
uv sync --all-groups --no-editable
```

Use `--no-editable` so the `equitytrace` CLI remains importable on macOS.
Python 3.12+ ignores `.pth` files marked `UF_HIDDEN`, which can happen with
editable installs under recent macOS provenance rules.

## 3. Initialize the database

```bash
uv run equitytrace init-db
```

Existing v0.1 / v0.2 / v0.3.0 databases are upgraded additively by the same command.

## 4. Ingest a company (live SEC)

```bash
uv run equitytrace ingest AAPL
```

## 5. Explore statements and valuation factors

```bash
uv run equitytrace filings AAPL
uv run equitytrace facts AAPL --concept Revenue
uv run equitytrace facts-as-of AAPL --date 2024-01-15
uv run equitytrace statements AAPL --period FY2023
uv run equitytrace factor AAPL revenue-growth --period FY2023
uv run equitytrace factor AAPL price-to-book --period FY2023
uv run equitytrace factors AAPL --period FY2023
uv run equitytrace db-info
```

FCF yield, P/E, P/S, and P/B use stored PIT market cap when prices and shares
are present. `--market-cap` is an optional override.

Optional multi-name ranking after ingesting more tickers:

```bash
uv run equitytrace rank \
  --tickers AAPL,MSFT,GOOGL \
  --factor price-to-book \
  --period FY2023
```

## 6. Optional market-data smoke (requires Twelve Data key)

```bash
# set EQUITYTRACE_TWELVE_DATA_API_KEY in .env first
uv run equitytrace market ingest AAPL --start 2020-01-01 --end 2024-12-31
uv run equitytrace market ingest SPY --asset-type etf --start 2020-01-01 --end 2024-12-31
uv run equitytrace market cap AAPL --date 2023-03-31
uv run equitytrace market analytics AAPL --as-of 2024-12-31 --benchmark SPY
uv run equitytrace market rank-metric momentum_12_1 AAPL MSFT --as-of 2024-12-31
```

SEC-only workflows do not need a market-data key. Beta ranking is refused.

## 7. Research backtest (offline fixtures or a local DuckDB)

```bash
uv run equitytrace portfolio backtest \
  --tickers AAPL,MSFT,GOOGL \
  --factor roa \
  --top-n 2 \
  --baseline equal-weight \
  --start 2020-01-01 \
  --end 2024-12-31 \
  --schedule monthly \
  --cost-bps 10
```

`--schedule explicit` takes `--explicit-dates YYYY-MM-DD,YYYY-MM-DD` that must
match stored calendar sessions (default calendar symbol SPY). Exit `2` means
the run is research-unavailable; exit `1` is a failed run.

## 8. Run the offline test suite

```bash
uv run pytest
```

## Optional quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## One-shot bootstrap

```bash
./scripts/bootstrap.sh
```
