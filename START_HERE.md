# Start here

Exact commands for a new developer on FilingEdge v0.1.

## 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set a real contact email:

```text
FILINGEDGE_SEC_EMAIL=you@example.com
FILINGEDGE_SEC_ORGANIZATION=FilingEdge
```

The SEC requires an identifying User-Agent for EDGAR fair access. Tests do **not** need this email.

## 2. Install dependencies

Requires [uv](https://github.com/astral-sh/uv) and Python 3.12+.

```bash
uv sync --all-groups --no-editable
```

Use `--no-editable` so the `filingedge` CLI remains importable on macOS.
Python 3.12+ ignores `.pth` files marked `UF_HIDDEN`, which can happen with
editable installs under recent macOS provenance rules.

## 3. Initialize the database

```bash
uv run filingedge init-db
```

## 4. Ingest a company (live SEC)

```bash
uv run filingedge ingest AAPL
```

## 5. Explore stored data

```bash
uv run filingedge filings AAPL
uv run filingedge facts AAPL --concept Revenue
uv run filingedge facts-as-of AAPL --date 2024-01-15
uv run filingedge db-info
```

## 6. Run the offline test suite

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
