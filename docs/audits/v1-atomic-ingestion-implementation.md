# EQUITYTRACE — ATOMIC INGESTION IMPLEMENTATION REPORT

**Verdict: BLOCKED — IMPLEMENTATION OR MEMORY GATE FAILED**

The first 25,000-fact memory qualification reached **8,395,874,304 bytes peak process RSS (7.82 GiB)** against a provisional **1 GiB** target. The bounded implementation is preserved locally, uncommitted. No PR was opened.

## Repository and publication state

- Base SHA: `0f48c0a12fc10665dc8f78f840084c7765728f45`.
- Final committed SHA: **unchanged at the base SHA**; implementation changes are uncommitted.
- Branch: `fix/v1-atomic-sec-ingestion`.
- Implementation worktree: `C:/Users/sh1ft/EquityTrace-atomic-sec`.
- PR URL: **none**. No commit, push, merge, tag or publication occurred.
- Intended base, if qualification is later completed: `audit/v1-independent-correctness`, not main.
- GitHub heads checked before implementation: #34 `0f48c0a`, #33 `f748fe8`, #30 `de2432a`, #19 `c67d865`. These PRs were not edited.
- The main checkout, its two earlier decision documents, the detached decision worktree and the original synthetic diagnostics were preserved.
- Version **1.0.0**, skfolio **1.2.3**, locked DuckDB **1.5.5**, pyproject.toml, uv.lock and Database.session are unchanged.

This is a failed qualification handoff, not a release-qualified implementation. The unavailable historical crash evidence was not searched for or reconstructed.

## Exact files changed

Modified production files:

- `src/equitytrace/repositories/ingestion.py`
- `src/equitytrace/repositories/facts.py`
- `src/equitytrace/repositories/filings.py`
- `src/equitytrace/sec/submissions.py`
- `src/equitytrace/financials/service.py`
- `src/equitytrace/factors/engine.py`
- `src/equitytrace/market/shares.py`
- `src/equitytrace/market/market_cap.py`
- `src/equitytrace/portfolio/engine.py`

Added production files:

- `src/equitytrace/transactions.py`
- `src/equitytrace/sec/validation.py`

Tests:

- Modified `tests/test_database.py`.
- Added `tests/test_atomic_ingestion.py`.

New qualification scripts:

- `scripts/sec_ingestion_synthetic.py`
- `scripts/qualify_sec_memory.py`
- `scripts/qualify_sec_recovery.py`

Documentation:

- `docs/audits/v1-atomic-ingestion-implementation.md` (this report).

Measurements and the synthetic database are outside the repository at
`C:/Users/sh1ft/AppData/Local/Temp/equitytrace-atomic-memory-25000/`.

## Transaction ownership and publication

Ingestion owns one explicit issuer transaction after fetch, normalization and typed validation. It writes issuer metadata, upserts the reported securities without deleting absent historical identities, replaces filings/facts, and updates that run's success/counts before COMMIT.

The initial running row is recorded separately. Writers through IngestionRepository are serialized per database path inside one process. DuckDB retains responsibility for inter-process file locking; no simultaneous multi-process reader/writer guarantee is claimed.

Pre-mutation checks reject mixed CIKs, missing/duplicate identifiers, invalid/nonfinite facts, malformed or ambiguous empty payloads, and omission of previously stored filing/observation history. Observation coverage excludes corrected values, allowing changed facts to retain their exact incoming fact IDs. SQL rows are constructed in batches of 500 filings and 2,000 facts. No large single-issuer ON CONFLICT fact operation or new schema was introduced.

Admitted payload limits are 250,000 observations, 50,000 filings and 128 MiB of text, with explicit rejection rather than truncation. These limits bound admitted normalization work; they do not prove a 1-GiB process limit, nor bound the SEC client's existing pre-validation JSON parsing. The qualification failed well below those row limits.

## Status and uncertain COMMIT outcomes

For an ordinary pre-COMMIT exception, the data transaction rolls back and failure is recorded on a fresh connection. If failure bookkeeping also fails, the original exception remains the chained cause and receives a diagnostic note.

After a COMMIT attempt errors, the writer connection scope closes before reconciliation. A persisted success is preserved and returned as successful publication. A still-running row can be reconciled to interrupted/failed under the same ingestion ownership. Missing or uninspectable state raises an explicit IngestionOutcomeUnknown. Subsequent failure bookkeeping is guarded so it cannot demote a committed successful row.

A success update must find its own running run record; otherwise publication fails before COMMIT. Stale running records are reconciled before a new API ingestion under the process lock. This bookkeeping does not certify or repair historical financial data.

Normal lost-acknowledgement tests cover both a COMMIT that happened and a COMMIT that did not happen. They do not prove durability after process termination or engine recovery.

## Reader consistency, retention, PIT and provenance

The opt-in snapshot helper detects an existing explicit transaction using two current_transaction_id probes. It joins caller ownership instead of issuing a nested BEGIN, and otherwise owns the logical read scope. Database.session remains unchanged.

Financial snapshots, factor calculations/all-factor calculations/rankings, shares selection and market-cap calculations/series use the helper. The backtest execution/read phase uses a shared scope that closes before portfolio persistence opens its own transaction. Formulas, mapping choices, availability filters and factor units were not changed.

Targeted tests publish a changed snapshot between metadata and fact reads, and between current/prior factor-period reads. The logical calculation retains its old coherent snapshot. A caller-owned transaction remains under caller control.

Rollback tests compare all four entity sets, including persistence timestamps and source provenance, against the previous snapshot. Identical and changed retries, historical-security retention, a second issuer, and exact availability boundaries are covered. Retention on failure does not add a historical archive after successful replacement.

Existing partial/corrupt databases are not automatically repaired or certified. Affected historical data must be validated and completely re-ingested, and dependent derived research recomputed, before being treated as trustworthy. Full migration qualification remains incomplete.

## Memory measurements and failed gate

Runtime: Windows AMD64, Python **3.12.14**, DuckDB **1.5.5**. Physical host memory reported by Windows: **67,737,919,488 bytes**, approximately **63.09 GiB**. This was not a physical 4-GiB host qualification.

The synthetic input has 25,000 facts, 125 filings, 256 concepts, unique observation provenance, descriptive concept text, accession/form/filing/acceptance metadata, and typed normalization through the actual ingestion pipeline.

Completed initial phase:

- Time: **59.36081009998452 seconds**.
- Peak process RSS: **8,395,874,304 bytes / 7.82 GiB**.
- Provisional target: **1,073,741,824 bytes / 1 GiB**.
- Synthetic database file size observed afterward: **13,119,488 bytes**.
- No WAL file was present in the final filesystem inventory.
- Process peak was independently corroborated by Get-Process PeakWorkingSet64.

During the next identical-ingestion phase, an OS sample showed **3,826,020,352 bytes current RSS** and the same peak. The repeat phase had not completed. The benchmark was stopped with Ctrl-C after the gate failure; its database was not reopened or inspected for recovery.

**Measurement limits:** the harness buffers Python tracemalloc, DuckDB tracked-memory, WAL and per-phase samples until its final JSON write. Those detailed samples were not persisted before the stop. Python/native attribution, peak WAL size, rollback memory, completed repeat latency and later retry memory are therefore **unavailable**. Do not infer them from total process RSS or the final file sizes.

The 100,000-fact run and the changed/rollback/retry memory phases were not executed/completed after this failure. No dataset was silently reduced, no checkpoint setting was changed and no alternate engine was tried.

Captured observations are preserved in `observed-failure.json` in the measurement directory. A companion implementation manifest records source hashes. These are new observations, not reconstructed historical evidence.

## Verification actually completed

- `uv lock --check`: PASS.
- `uv sync --all-groups --no-editable --locked`: PASS.
- `uv run ruff check .`: PASS on the resulting source/scripts/tests.
- Strict `uv run mypy src`: PASS, 75 source files.
- Targeted pytest: **62 passed** (37 new atomic-ingestion cases, 8 database cases, 14 factor cases, 3 PIT cases).
- `git diff --check`: PASS.
- Recovery harness: `--plan` only, reporting no termination authorization and no execution.

The targeted tests include changed/identical replacement, pre-transaction failures, each write stage, an inter-batch failure, old-snapshot retention, atomic data/success visibility, definite and uncertain outcomes, invalid/empty/history-shrinking input, a second issuer, security retention, multi-statement reader/factor consistency, availability boundaries, caller-owned transaction preservation and normal existing-file reopen/initialize.

**Not completed after the memory gate failed:** full offline pytest, the three determinism seeds, a final Ruff format-check command, uv build, wheel/sdist package smoke, the complete migration suite, 100,000-fact qualification, or any CI run. Formatting was applied during development, but this is not reported as the requested final format-check gate. No five-job CI claim is made because there is no pushed implementation SHA or PR.

No credentials or live SEC/market providers were used. No caches or generated databases were staged or committed.

## Crash harness prepared versus executed

The recovery script prepares before-COMMIT, during-COMMIT and post-acknowledgement arms, a fresh-process minimal reopen/close, a second fresh-process verifier, full-content comparison, an actually indexed lookup versus a scan oracle, and identical/changed retries.

It requires an explicit `--execute-authorized` switch; only `--plan` was run. The during-COMMIT arm is timing-sensitive and must be independently reviewed before authorization; the scaffold is not itself qualified crash evidence.

Stopping the memory benchmark was an operational stop at a failed gate, not a recovery experiment. No recovery assertions were run against that interrupted database. The earlier engine anomaly remains unconfirmed.

## Remaining blockers and next action

The **memory gate failed**, and the broader local/CI/recovery gates remain incomplete. A normal 25,000-fact transaction already exceeded the target by roughly 7.82 times on this host. A passing targeted suite cannot override this result.

**Next action:** independently review the preserved memory failure and uncommitted Option A implementation before authorizing further persistence changes. Do not push a draft PR, change engine settings/dependencies, switch architectures, or execute the crash harness on the strength of this run.

