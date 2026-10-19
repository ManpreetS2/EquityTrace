# EquityTrace

**Transparent quantitative equity research with point-in-time evidence,
reproducible strategies, and offline HTML reports.**

EquityTrace turns SEC filings and market history into traceable financial
statements, fundamental factors, and research backtests you can validate, run,
inspect, and export—without a web app or broker.

Latest published release: **v0.3.2**. This tree prepares **v1.0.0** (release
candidate / not yet published as a GitHub Release).

## Core capabilities

- SEC/XBRL ingestion with issuer vs security identity
- Point-in-time statements filtered by `available_at <= as_of`
- Traceable fundamental and valuation factors + rankings
- Market bars, PIT market cap, momentum / volatility / beta / drawdown
- Native weight-return portfolio research (equal weight, inverse vol, min-variance)
- Typed research strategies with economic fingerprints
- Standalone HTML reports and deterministic JSON exports from persisted runs

## Why point-in-time matters

A backtest that treats a fiscal period-end as the date a number became known
silently cheats. EquityTrace stores `available_at` for filings/facts and
filters:

```text
available_at <= as_of
```

Research decisions use that evidence boundary. Provider-adjusted prices are
still **not** vendor-vintage point-in-time price history—see limitations.

## Quickstart: offline synthetic demo

No SEC email, Twelve Data key, or internet required.

```bash
uv sync --all-groups --locked
uv run python scripts/demo_research.py
```

Open the printed `research_report.html` path in a browser. Details:
[examples/demo/README.md](examples/demo/README.md).

The demo uses **synthetic** AAA/BBB evidence. NAV paths are research
illustrations, **not** historical investment performance and **not** advice.

## Research workflow

```text
strategy JSON
  -> equitytrace research validate
  -> equitytrace research run        # BacktestEngine + DuckDB persist
  -> equitytrace research show RUN_ID
  -> equitytrace research report RUN_ID --output report.html
  -> equitytrace research export RUN_ID --output export_dir/
```

Strategy **name/description** live in the JSON file only. Durable identity is
`run_id` plus the SHA-256 economic fingerprint of the canonical
`BacktestRequest`.

## Architecture overview

```text
SEC / market evidence
  -> DuckDB (issuers, securities, facts, bars, portfolio_* tables)
  -> factors / ranking / market analytics
  -> portfolio BacktestEngine (weights, drift, costs, audit)
  -> research CLI + reporting (HTML / JSON)
```

Module map: [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md).
Formulas: [docs/metrics.md](docs/metrics.md).
Data model: [docs/data-model.md](docs/data-model.md).

## Financial correctness (summary)

- Facts: `available_at <= decision_at` / `as_of`
- Latest eligible FY per security/ticker; no silent older-year fallback
- Exact top-N; ranking direction from the factor registry
- Same-issuer multi-security universes fail closed (no share-class guess)
- Decision after session availability; target effective next calendar session
- Drift, gross turnover, and native bps costs (not broker fills)
- Min-variance adapter supplies **target weights only** (CLARABEL; 252 common returns)
- Standing warnings include adjusted-history and survivorship caveats

## Technologies

Python 3.12 · DuckDB · Pydantic · Typer · uv · skfolio 1.2.3 (min-variance only)

## Installation and live data

```bash
uv sync --all-groups --locked
cp .env.example .env   # SEC email; Twelve Data key only for live market ingest
uv run equitytrace init-db
```

Live SEC/market commands need configuration documented in
[START_HERE.md](START_HERE.md). The offline demo does not.

## Testing and reproducibility

```bash
uv run pytest
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

CI also runs package smoke and `PYTHONHASHSEED` determinism checks. For one
persisted run, HTML and JSON exports regenerate byte-identically.

## Limitations and non-goals

- Not investment advice; no guaranteed correctness or profitability
- Provider-adjusted history ≠ vendor-vintage PIT prices
- User universes may embed survivorship bias
- No share/fill execution ledger, intraday book, or broker APIs
- No web UI / Streamlit / FastAPI
- No additional optimizer families beyond min-variance
- No historical constituent database
- v1.0.0 is prepared here but **not** tagged/published until explicit approval

## License

MIT
