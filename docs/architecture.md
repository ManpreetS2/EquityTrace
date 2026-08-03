# Architecture (v0.1)

FilingEdge v0.1 is a layered Python application with a clear boundary between SEC I/O, normalization, persistence, and CLI.

## Layers

1. **CLI (`filingedge.cli`)**
   Typer commands with Rich output. Converts user errors into readable messages (no stack traces for expected failures).

2. **Configuration (`filingedge.config`)**
   Environment-driven settings via pydantic-settings. Builds the SEC User-Agent from organization + contact email.

3. **SEC client (`filingedge.sec.client`)**
   HTTPX client with:
   - identifying User-Agent
   - rate limiting below 10 req/s
   - retries with exponential backoff for timeouts / 429 / 5xx
   - optional disk cache
   - injectable transport for offline tests

4. **Endpoint helpers**
   - `tickers.py` — company_tickers.json resolution
   - `submissions.py` — primary + archived submissions merge
   - `company_facts.py` — XBRL company facts fetch
   - `normalization.py` — typed domain records + `available_at`

5. **Domain models (`filingedge.models`)**
   Pydantic models: `Issuer`, `Security`, `Filing`, `FinancialFact`, run summaries.

6. **Persistence**
   - `database.py` — DuckDB path + idempotent schema
   - repositories for issuers, securities, filings, facts, ingestion
   - atomic company snapshot writes inside a transaction

## Point-in-time design

```text
acceptanceDateTime (Eastern) -> UTC  => available_at
if missing: end of filing_date (Eastern) -> UTC => available_at
```

Queries:

```python
FactsRepository(...).get_facts_as_of(ticker="AAPL", as_of=dt)
# SQL predicate: available_at <= as_of
```

Report period `end` dates are never used as availability.

## Offline testing

`httpx.MockTransport` serves fixtures from `tests/fixtures/sec/`. No live SEC calls in CI.
