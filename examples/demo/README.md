# EquityTrace demo (synthetic data)

Two offline demonstrations share the same engine. **Everything here is fictional.**
No SEC email, no Twelve Data key and no internet access are needed.

| Demo | Command | What it is |
| --- | --- | --- |
| Interactive research demo | `uv run --extra demo python scripts/launch_demo.py` | Local Streamlit UI over a seven-issuer synthetic dataset |
| Command-line smoke fixture | `uv run python scripts/demo_research.py` | Four sessions, two issuers, writes an HTML report and JSON exports |

## Interactive demo

```bash
uv run --extra demo python scripts/launch_demo.py
```

Open <http://127.0.0.1:8501>. The server binds to `127.0.0.1` only and Streamlit
usage statistics are disabled. Useful flags: `--port`, `--no-browser`,
`--directory DIR`, `--prepare` (build the dataset before starting),
`--prepare-only` (build and exit) and `--rebuild` (rebuild; deletes saved demo runs).

`uv sync --all-groups --locked` also installs the UI (the lock file contains the
`demo` extra and group). Without Streamlit the launcher prints the install command.

### Ten-step walkthrough (manual verification checklist)

1. **Start** the app with the command above. The overview shows a red
   "not prepared" state and a prominent `SYNTHETIC DEMO DATA` banner.
2. **Click *Load synthetic demo*.** The database is built in a few seconds under
   `data/demo/` (gitignored). Nothing is created until you click.
3. **Data explorer:** pick `SYBR`, set the as-of date to 2024-03-13, then use the
   *As-of comparison* tab ("Use the SYBR example"). FY2023 statements are
   *Unavailable* on the 13th and on the 14th at 16:15 ET, and appear from the 15th,
   because the 10-K is accepted on 2024-03-14 at 16:30 ET.
4. **Strategy builder:** choose a preset (for example *ROA quality · top 3 · equal
   weight · monthly*) and press *Load preset into the form*.
5. **Modify** a parameter (top-N, factor, baseline, schedule, cost). Nothing runs
   when a field changes.
6. **Validate:** use *Validate strategy*, *Preview canonical JSON* and *Show economic
   fingerprint*. Invalid input (for example top-N = 0) shows the validation message
   and disables *Run backtest*.
7. **Run backtest.** This is the only action that executes research. A second click
   on the unchanged strategy opens the saved run; *Run again* creates a new one.
8. **Results / Inspect decisions:** read NAV, drawdown, benchmark, turnover, costs and
   target allocations; then pick a rebalance and compare each source filing's
   *Public at* time with the decision time.
9. **Reports & exports:** download the standalone HTML report.
10. **Download `request.json` and `result.json`** and compare them with the CLI
    (`research show` / `research export`, below). Regenerating a file from the
    same saved run gives identical bytes.

Then try the four *Diagnostic* presets: they end **UNAVAILABLE** with an explanation
(two share classes of one issuer, valuation factor without share counts, optimizer
without enough price history, prior-year evidence not yet public). No performance is
shown for them.

### The synthetic dataset

* 7 fictional issuers (CIKs `0990000001`–`0990000007`, a range the SEC has never
  assigned) and 8 securities; `SYGA` and `SYGB` are two classes of one issuer.
* Three fiscal years (FY2021–FY2023) of 10-K facts per issuer, filed after each fiscal
  year ends (for example FY2023 in February–March 2024). Facts become usable only
  from the filing's acceptance time (`available_at`).
* Daily prices 2022-07-01 to 2024-12-31 for the securities and a fictional benchmark
  `SYNX`, generated from a fixed-seed integer generator. The history is long enough
  for the 253 aligned closes that inverse-volatility and minimum-variance weighting
  need.
* Prices are generated **independently of the fundamentals**. No factor has a
  built-in edge, so strategies can lose money or trail the benchmark. That is the
  honest outcome.
* Bars are stored under the only supported provider label (`twelve_data`) because the
  provider enum has no other member; every bar's `source_metadata_json` says
  `synthetic: true`.
* The content is pinned by a SHA-256 digest in `tests/test_demo_data.py`.

### Same results from the command line

The UI and the CLI use the same models and services. After the dataset exists
(`--prepare-only` creates it without Streamlit):

```bash
uv run python scripts/launch_demo.py --prepare-only
uv run equitytrace research validate --strategy examples/demo/strategies/roa-equal-monthly.json
uv run equitytrace research run --strategy examples/demo/strategies/roa-equal-monthly.json \
    --database data/demo/equitytrace_demo.duckdb
uv run equitytrace research show RUN_ID --database data/demo/equitytrace_demo.duckdb
uv run equitytrace research export RUN_ID --output out --database data/demo/equitytrace_demo.duckdb
```

`examples/demo/strategies/*.json` are the same seven presets the UI offers. On Windows,
set `PYTHONIOENCODING=utf-8` if you pipe the CLI output.

### Safety rules the demo follows

* The demo database is separate from your own. The demo ignores
  `EQUITYTRACE_DATABASE_PATH`; its directory is `data/demo/` or `EQUITYTRACE_DEMO_DIR`.
* Only a database carrying the demo's marker manifest can be replaced, and only on an
  explicit rebuild. Re-loading an intact demo changes nothing and keeps saved runs.
* A failed build leaves no trusted partial dataset.
* Reruns, navigation, result inspection and downloads never prepare data or run a
  backtest. No DuckDB connection is cached across interactions.

### Limits

Hypothetical simulation; no live trading; non-vintage adjusted prices; fixed user
universe (survivorship); synthetic evidence only. This demo does **not** certify live
SEC ingestion capacity or crash recovery, which remain separate release gates.

## Command-line smoke fixture

```bash
uv run python scripts/demo_research.py
```

Seeds a temporary DuckDB with two fictional issuers, FY2022 facts filed in March 2023
(prior-year comparatives in March 2022) and four January 2024 sessions, then runs the
real `BacktestEngine`, persists and reloads the run, and writes
`examples/demo/out/research_report.html` plus `export/request.json` / `result.json`
(gitignored). Pass `--output-dir` to choose another directory and `--keep-db` to keep
the temporary DuckDB.

## Packaging note

Both demos are **source-checkout oriented**. A wheel/sdist install ships the
`equitytrace.demo` and `equitytrace.ui` modules but not `scripts/` or `examples/`.
Use a clone of this repository to run them.
