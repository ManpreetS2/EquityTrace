# Offline research demo (synthetic evidence)

This demonstration seeds a **temporary** DuckDB with clearly labeled synthetic
issuers (`AAA`, `BBB`), fiscal-year ROA facts, and adjusted prices, then runs
the real EquityTrace research path:

1. validate strategy JSON
2. execute `BacktestEngine`
3. persist and reload the run
4. generate a standalone HTML report
5. export deterministic `request.json` / `result.json`

It requires **no SEC email**, **no Twelve Data key**, and **no internet**.

## Important labels

- Evidence is **synthetic**, not downloaded SEC or vendor history.
- NAV / return paths are research illustrations, **not historical investment
  performance**.
- Outputs are **not investment advice**.

## Run (source checkout)

From the repository root, after `uv sync --all-groups --locked`:

```bash
uv run python scripts/demo_research.py
```

Optional output directory:

```bash
uv run python scripts/demo_research.py --output-dir examples/demo/out
```

The script prints the absolute path of `research_report.html`. Open that file
in a browser (offline; no server required).

Generated files land under `examples/demo/out/` (gitignored):

- `research_report.html`
- `export/request.json`
- `export/result.json`

Temporary DuckDB files are deleted automatically unless `--keep-db` is passed.

## Packaging note

This demo is **source-checkout oriented**. A wheel/sdist install of EquityTrace
does not claim to ship `scripts/demo_research.py` or `examples/demo/` as an
installed console entry point. Use a clone of this repository for the demo.

## Negative diagnostic path

The automated suite also asserts that a persisted UNAVAILABLE/FAILED research
result never renders as a successful performance story in the HTML report.
See `tests/test_demo_research.py`.
