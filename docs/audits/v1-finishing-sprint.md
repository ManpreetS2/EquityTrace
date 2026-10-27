# EquityTrace v1.0 finishing sprint

Release remains BLOCKED on resource qualification and the separately authorized crash
gate. Starting head: `a1819a421880c81deeefb6453cd535ebe6229e52`, draft PR #35,
stacked on `audit/v1-independent-correctness`. Exact final SHA/CI are in the handoff.

## Recovery preparation and correctness

The harness checks complete issuer, security, filing, fact and run state against old/new
oracles, including PIT, cross-issuer data, retained securities and identical/changed retries.
The first data SELECT is an actually profiled CIK lookup, before any full-table scan.
Independent unfiltered scans derive the same selective views; required indexed paths must
have actual index plans. Alternate recovery-open sequences use separate databases.
Diagnostic planner thresholds are not production workarounds.

Pre-COMMIT holds precede the native call. Acknowledgement follows returned COMMIT. The
call-adjacent arm remains ambiguous pre-/during-COMMIT, including possible return before
the acknowledgement marker is durable. Acknowledgement races leave coverage INCONCLUSIVE.
Marker identities, names, distinct ordinals and ordering are checked. Source/dependency
hashes and failure manifests are preserved. Existing output directories are not overwritten;
failure-evidence errors do not replace the original exception. Default invocation prints
a plan; six-case self-checks are normal-only. Timeout handling never automatically terminates
children. Termination mode was not executed and requires separate explicit authorization.

One production defect was reproduced with a failing regression: a malformed supplied fact
start date became None in normalization and could publish as an instant observation on
first ingestion. Admission now parses non-null supplied starts before normalization.
Regressions cover both first ingestion and retention of an existing complete snapshot.
Legitimately missing starts and valid duration dates remain unchanged.

The bounded review also covered the entire PR's publication/status handling, typed/batched
writers, complete-history retention, historical securities, PIT/restatement selection,
reader ownership, backtest read/persist separation and compatibility tests. No other
objectively reproduced defect was found. This is not crash or general engine certification.
Success records do not certify superseded snapshots or repair pre-existing partial data.

## Finite memory matrix

Three fresh Windows subprocesses were pre-recorded: maximum 25,000 live facts, 5,000
historical run rows, initial/identical/changed/later-batch rollback/retry, and a 1-GiB
high-water RSS ceiling. JSON generation/parsing, typed construction, insertion, COMMIT,
close and combined full-domain verification/reconciliation were observed. tracemalloc
and fsynced JSONL logging add overhead; none was subtracted. Stops are cooperative at
measurement boundaries, not hard OS reservations or prevention of intra-step peaks.

- Baseline: 25,000 target facts, 256 concepts, 125 filings. Peak RSS 655.18 MiB;
  111.11 seconds; all five operations passed. Python allocation peak 191,243,533 bytes;
  maximum tracked DuckDB memory 126,574,592 bytes; DB 29,110,272 and WAL 17,142,180 bytes.
- Large text: 25,000 facts, 4,096-byte descriptions and 512-byte labels;
  expanded typed fact strings 119,500,000 bytes. Stopped in initial insertion batch 8
  at 1,032.27 MiB in 10.93 seconds. Python peak 296,425,584 bytes; tracked DuckDB
  maximum 222,504,960 bytes, including overflow strings and transaction-local tables.
  Normal reopen found zero issuer/security/filing/fact rows and 5,001 failed runs
  (5,000 historical plus the candidate): no partial successful snapshot was published.
- Diverse: 20,000 target facts, 2,048 concepts, two units, 500 target filings, plus five
  unrelated 1,000-fact issuers: 25,000 facts total. Peak RSS 784.05 MiB; 109.19 seconds;
  all five operations passed. Python peak 174,853,704 bytes; tracked DuckDB maximum
  273,588,224 bytes; DB 31,731,712 and WAL 26,233,822 bytes.

These maxima occur at different boundaries and must not be added as simultaneous memory.
Untracked RSS is not proof of an engine leak. The measured script matched the pre-recorded
hash and is preserved externally. Its subsequent stricter rollback-error check changes
neither valid outcomes nor the preserved measurements; the matrix was not rerun.

Passing profiles exercised guarded reconciliation with 5,000 historical runs. Combined
oracle/query/reconciliation windows took 12.54–14.01 seconds under tracemalloc, not isolated
guard timings, and did not exceed earlier process peaks. Current RSS sometimes rose;
this short sequence does not establish absence of long-term native allocator retention.
Python scan buffers are bounded at 500 rows, but full-history scans/sorts per candidate
and paged run enumeration add engine I/O/sort cost as history and issuer count grow.

Host RAM was roughly 63 GiB, with no enforced 4-GiB environment. No workload above 25,000
facts ran. The prior 651.47-MiB qualification remains separate historical evidence.

## Capacity decision

No admission change is justified. A row-count cap alone does not bound parsed JSON,
expanded descriptions or native memory. Current 250,000-observation / 50,000-filing /
128-MiB traversed-text checks remain unchanged and do not constitute a qualified envelope.
Response/cache JSON is materialized before admission. Normalization constructs row dicts,
a Polars frame, `to_dicts()` output, typed facts, coverage sets and bounded SQL arrays.
Repeated text can be duplicated in those intermediates despite modest raw text size.

Do not truncate history or reduce provenance. A synthetic text cap would not establish
representative issuer coverage. The next capacity decision needs an explicit supported
input contract and representative offline fixtures, rather than an open-ended optimization
search. Capacity and crash qualification are independent release blockers.

## UI assessment: recommendation only

The complete plans were read at PR #36's exact head without modifying it. The October 2026
frozen scope prioritizes existing research reports/exports; old strategy-builder wording
does not approve a browser UI automatically. Trading, forecasting and Financial Modeling
Lab remain separate projects.

Verified current functionality is CLI SEC/market ingestion and facts/as-of/statements/
factor/ranking inspection; frozen ResearchStrategy JSON and economic fingerprints;
typed BacktestRequest; deterministic drift/cost/audit backtests and persisted results;
`research validate|run|show|report|export`; standalone HTML charts, methodology, warnings
and evidence; deterministic request/result JSON. Report rendering reads persisted results
without silently rerunning the engine. There is no frontend/server API. Strategy display
name/description are not persisted economic identity: archive the original strategy JSON.

Recommend CLI + HTML + JSON for v1: validate local evidence, inspect statements/factors
at an explicit as-of time, validate strategy/fingerprint, explicitly run a backtest,
inspect status, open its report and archive strategy/request/result. Complexity: Small
documentation/usability qualification, with no dependency or product-runtime addition.

A thin Streamlit alternative is a separately approved Medium milestone: local-only
statement/factor inspection, typed strategy form/JSON validation with explicit Run action,
and persisted-run/report/export browsing. A read-only report viewer alone is Small but
does not fulfill a strategy-builder promise. Reuse FinancialsService, FactorEngine,
ResearchStrategy, BacktestEngine, PortfolioRepository and reporting functions through a
small Python boundary; do not duplicate SQL, financial logic or engine behavior in UI code.

Streamlit reruns on interaction; globally cached resources are shared across sessions
and must be thread-safe. Do not globally cache a mutable DuckDB connection or trigger
ingestion/backtests on reruns. Use owned short-lived connections/snapshots, explicit actions,
duplicate-action guards, and immutable cache entries keyed by as-of/data/run identity.
Preserve explicit single-process writer ownership; no multiprocess write promise or
automatic provider refresh. Official references:

- [Execution/caching model](https://docs.streamlit.io/develop/concepts/architecture/caching)
- [Shared resource semantics](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.cache_resource)

Either interface must expose unavailable reasons instead of zeros, exact as-of timestamps,
period/accession/available-at/source evidence, run/fingerprint/version, optimizer/audit
status, drift/cost/next-session effectiveness and non-vintage/universe limitations.
Tests require backend/CLI equivalence, PIT/restatement boundaries, share-class ambiguity,
rerun idempotence, stale-cache invalidation, transaction ownership, failed runs, escaped
HTML, deterministic downloads and bounded memory. No frontend code was implemented.

Explicit product decision required: accept CLI/HTML as the v1 interface, or approve a
bounded local Streamlit milestone and its dependency/write/concurrency scope separately.
The recommendation here does not silently change approved product requirements.

## Verification and remaining gates

Final offline quality, determinism, package and CI results are tied to the exact pushed
SHA in the handoff. Raw observations, disposable DBs, pre-recorded matrix, manifests,
the original failing regression and prior bundles remain external and are not committed.
Release remains BLOCKED on demonstrated resource failure and incomplete crash qualification.
No forced termination, crash tests, dependency/version change, frontend, protected-branch
change, merge, tag or release was performed. Independent review is the next action.
