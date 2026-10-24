# Start here

Exact commands for EquityTrace **v1.0.0** (release candidate in this tree).
Latest **published** GitHub Release remains **v0.3.2** until v1.0.0 is explicitly
tagged and published after review.

Read [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) first. Then skim
[docs/architecture.md](docs/architecture.md) and [docs/metrics.md](docs/metrics.md)
before searching the tree.

For a zero-credential walkthrough, run the offline synthetic demo first:

```bash
uv sync --all-groups --locked
uv run python scripts/demo_research.py
```

See [examples/demo/README.md](examples/demo/README.md).

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

Existing v0.1 / v0.2 / v0.3.0 / v0.3.1 databases are upgraded additively by the
same command.

## 4. Ingest a company (live SEC)

```bash
uv run equitytrace ingest AAPL
```

The research strategy in §7 (`examples/strategies/basic_quality.json`) ranks
**AAPL, MSFT and GOOGL** with `top_n: 2`. If fewer than two names have valid
factor values the run is UNAVAILABLE (exit `2`), so ingest all three now:

```bash
uv run equitytrace ingest MSFT
uv run equitytrace ingest GOOGL
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
uv run equitytrace market ingest MSFT --start 2020-01-01 --end 2024-12-31
uv run equitytrace market ingest GOOGL --start 2020-01-01 --end 2024-12-31
uv run equitytrace market ingest SPY --asset-type etf --start 2020-01-01 --end 2024-12-31
# Market cap and valuation factors need verified split coverage (see below):
uv run equitytrace market ingest-splits AAPL --start 2020-01-01 --end 2024-12-31
uv run equitytrace market cap AAPL --date 2023-03-31
uv run equitytrace market analytics AAPL --as-of 2024-12-31 --benchmark SPY
uv run equitytrace market rank-metric momentum_12_1 AAPL MSFT --as-of 2024-12-31
```

SEC-only workflows do not need a market-data key. Beta ranking is refused.

Live ingestion makes several Twelve Data calls (four `market ingest` commands
above, plus one `ingest-splits` per ticker you value). The free tier is rate
limited; if a command reports a rate-limit error, wait a minute and re-run it.
`ingest-splits` uses Twelve Data `/splits`, which needs a paid plan; without it,
market cap and the valuation factors that depend on it are reported unavailable
(never silently wrong). The §7 strategy (`basic_quality_score`) does not use
market cap, and it needs the SPY and per-ticker bars from `market ingest`.

Every `--as-of` / `--date` cutoff uses one rule: a date-only value means the
end of that day in U.S. Eastern time, a datetime without a zone is U.S. Eastern,
and an explicit `Z` or offset is honored as given.

## 7. Research strategy and reports (v1.0)

Validate a typed strategy JSON without opening DuckDB:

```bash
uv run equitytrace research validate \
  --strategy examples/strategies/basic_quality.json
```

Run and show use the same BacktestEngine / portfolio persistence as before.
Strategy **name** is file metadata only; durable identity is `run_id` plus the
economic fingerprint recomputed from the persisted request.

```bash
uv run equitytrace research run --strategy examples/strategies/basic_quality.json
uv run equitytrace research show RUN_ID
```

Exit codes for `research run`: `0` SUCCESS, `2` UNAVAILABLE, `1` FAILED.

Generate a standalone HTML report or deterministic JSON exports from a
**persisted** run (read-only DuckDB; no engine, SEC, or Twelve Data calls):

```bash
uv run equitytrace research report RUN_ID --output reports/research.html
uv run equitytrace research export RUN_ID --output exports/RUN_ID
```

Report/export exit `0` means the artifact was written; research status may still
be UNAVAILABLE/FAILED. Diagnostic reports must not invent success metrics.
Strategy name is not persisted; the report shows the economic fingerprint and
stored package version.

## 8. Research backtest (offline fixtures or a local DuckDB)

`equitytrace portfolio backtest` remains available and unchanged:

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
match stored calendar sessions (default calendar symbol SPY). Duplicate dates
collapse to the first occurrence; remaining dates are executed in chronological
order. Every remaining date must sit in `[start, end]`, exist as a stored
calendar session, and have a later effective session on or before `--end` —
otherwise the run is unavailable rather than quietly skipping a date. The CLI
always runs with `adjustment_mode=all`; v0.3c native backtests currently require
that provider-adjusted research path. Exit `2` means the run is
research-unavailable; exit `1` is a failed run.

## 9. Run the offline test suite

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
