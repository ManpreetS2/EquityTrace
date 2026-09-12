# Start here

Exact commands for a new developer on EquityTrace v0.3.0 (v0.3a market-data foundation).

Repository navigation: [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md). Read that
map before searching the tree; it names the bounded files for each subsystem.

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

Existing v0.1 databases are upgraded additively by the same command.

## 4. Ingest a company (live SEC)

```bash
uv run equitytrace ingest AAPL
```

## 5. Explore stored data and statements

```bash
uv run equitytrace filings AAPL
uv run equitytrace facts AAPL --concept Revenue
uv run equitytrace facts-as-of AAPL --date 2024-01-15
uv run equitytrace statements AAPL --period FY2023
uv run equitytrace factor AAPL revenue-growth --period FY2023
uv run equitytrace factors AAPL --period FY2023
uv run equitytrace db-info
```

Optional multi-name ranking after ingesting more tickers:

```bash
uv run equitytrace rank \
  --tickers AAPL,MSFT,GOOGL \
  --factor revenue-growth \
  --period FY2023
```

## 6. Optional market-data smoke (requires Twelve Data key)

```bash
# set EQUITYTRACE_TWELVE_DATA_API_KEY in .env first
uv run equitytrace market ingest AAPL --start 2023-01-01 --end 2023-03-31
uv run equitytrace market cap AAPL --date 2023-03-31
```

SEC-only workflows do not need a market-data key.

## 7. Run the offline test suite

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
