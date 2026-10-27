> Historical report preserved for handoff. Its recommendations, counts and local paths describe the reviewed state at that time. Current status is in [DEVELOPMENT_HANDOFF](../DEVELOPMENT_HANDOFF.md). Transactional ingestion and the reconciliation guard are now implemented; crash and resource qualification remain pending.

# EQUITYTRACE — FINAL INGESTION DECISION

**Preferred solution: A — narrowly bounded transactional replacement. Complexity: Medium. Release status: BLOCKED pending implementation and qualification.**

This selects an engineering direction for independent ChatGPT review, not a release-qualified fix. It supersedes the earlier provisional preference for snapshot generations. No production implementation was made.

## 1. Verified repository state

- Attached checkout: `C:/Users/sh1ft/EquityTrace`, `main`, HEAD `c368864f8bb9d4f5fc37c1e2731b95585a07bfd1`. Its pre-existing untracked `docs/audits/v1-persistence-decision.md` was preserved.
- Reference reviewed and executed: `0f48c0a12fc10665dc8f78f840084c7765728f45`, in a new detached temporary worktree: `C:/Users/sh1ft/AppData/Local/Temp/equitytrace-ingestion-decision-0f48c0a`. No existing branch was moved.
- Reference metadata: version **1.0.0**, skfolio **1.2.3**, locked DuckDB **1.5.5**; package requirement `duckdb>=1.1.0`. Attached main still declares 1.0.0.dev0; it was not changed.
- Read-only GitHub checks: [#19](https://github.com/ManpreetS2/EquityTrace/pull/19) open, release head `c67d865`; [#30](https://github.com/ManpreetS2/EquityTrace/pull/30) open, split-safe cap head `de2432a`; [#33](https://github.com/ManpreetS2/EquityTrace/pull/33) draft/open, integration head `f748fe8`; [#34](https://github.com/ManpreetS2/EquityTrace/pull/34) draft/open, accounting head `0f48c0a`, targeting the integration branch. No PR, release branch or dependency was modified.

The old diagnostic evidence remains unavailable. Its recovery search is closed and was not repeated. The following experiment is new evidence about committed application behavior, not reconstruction of historical crash results.

## 2. Minimal demonstrated defect

**Verified:** a failed refresh can expose a partially replaced issuer snapshot after normal connection close and reopen.

One small diagnostic, parametrized at two exception boundaries, uses the unchanged writer and normal schema/indexes. It seeds a synthetic CIK `0000000001`, ticker `SYN`, one issuer, one security, one filing and two facts. Fetch/normalization functions return in-memory synthetic objects; no provider or network is involved.

A connection proxy raises immediately before, or immediately after, the actual `DELETE FROM financial_facts WHERE cik = ?`. In the second case it first executes the real DELETE, then raises. This tests a precise application statement boundary; it is not an engine failure simulation.

Both cases start from:

- Counts, in issuer/security/filing/fact order: **1 / 1 / 1 / 2**.
- Revenue **100**, operating income **20**, accession `0000000001-24-000001`, source `https://synthetic.invalid/OLD`.
- Fiscal period FY2023, January 1–December 31, 2023. Filing/acceptance/availability February 1, 2024, 21:00 UTC.
- One recorded successful ingestion. Reads use April 1, 2024, 00:00 UTC as the knowledge cutoff.

Intended replacement: revenue **200**, operating income **50**, accession `0000000001-24-000002`, source `https://synthetic.invalid/NEW`, availability March 1, 2024, 21:00 UTC.

| Observed after failed refresh and reopen | Exception before fact DELETE | Exception after fact DELETE |
| --- | --- | --- |
| Issuer/security/filing/fact counts | 1 / 1 / 1 / 2 | 1 / 1 / 1 / 0 |
| Issuer name and security title | NEW | NEW |
| Filing accession | …000002 | …000002 |
| Fact values and accession | Old 100 and 20; …000001 | No facts |
| FinancialsService revenue | 100, old provenance, no revenue warning | Missing; missing_value warning |
| Ingestion run statuses | success, failed | success, failed |

**Distinction:** the first case retains stale facts but publishes mixed old/new entities; it is not a coherent retained old snapshot. The second loses both previously stored facts and publishes replacement metadata without facts. Missing-value warnings in the second case do not make the snapshot atomic. Neither case publishes the intended complete new snapshot. In both cases normal repository readers and FinancialsService can query the resulting state despite the failed run.

This does not demonstrate a wrong numeric factor after the second failure, a crash-recovery fault, an ART defect, or permanent media loss. It demonstrates committed application-level deletion and partial publication.

**Execution:** Windows AMD64, Python **3.12.14**, DuckDB **1.5.5**, pytest **9.1.1**. An existing interpreter was reused without installing or changing dependencies, with imports resolved to the reference worktree by pytest. Two new diagnostic cases and three focused existing tests passed: **5/5**, exit 0. The existing cases cover repairability, pre-persistence fetch failure, and a small explicit-transaction save. They do not qualify a transactional replacement fix.

**Evidence directory:** `C:/Users/sh1ft/AppData/Local/Temp/equitytrace-ingestion-decision-0f48c0a/decision-evidence/`. It contains `before_delete.json`, `after_delete.json`, the two closed synthetic DuckDB files, and `manifest.json`. Full fact IDs, values, provenance and run IDs are in the JSON. The diagnostic source is `tests/test_decision_exception_diagnostic.py` in that worktree. The manifest records SHA-256 hashes and the exact test selection. Preserve this directory; rerunning the diagnostic intentionally refuses to overwrite its databases.

## 3. Current persistence and reader architecture

**Writer:** IngestionRepository resolves the ticker, records a running run, fetches and normalizes, then independently persists issuer, securities, filings and facts. Success/failure bookkeeping uses separate connections. Database.session closes its connection; it does not begin or roll back a multi-statement transaction.

**Repositories:** IssuersRepository and SecuritiesRepository use upserts. FilingsRepository deletes by CIK and inserts in batches of 500. FactsRepository does the same in batches of 2,000. Both first build a full Python rows list and both return early for empty input. Mixed-CIK input takes a different upsert path. These are important memory and replacement-contract details.

**Schema:** primary keys identify CIK, ticker/CIK, accession and fact ID; SEC children reference issuers. Explicit secondary indexes cover security ticker/CIK, filing CIK/form/availability and fact CIK/concept/availability/accession. There is no active generation or per-snapshot publication relation.

**Readers:** FactsRepository joins securities and filters facts by available_at. FinancialsService resolves the security, loads facts and constructs canonical values; annual-period enumeration also queries raw tables directly. FactorEngine uses FinancialsService and market-cap/share readers, sometimes across several periods. Shares selection and market repository code also query raw SEC tables. No path makes a failed-run status a visibility barrier. Connection lifetime alone does not pin a consistent snapshot across multiple SELECT statements.

**Existing transaction ownership:** the market and portfolio repositories already open explicit transactions for certain writes. Globally adding BEGIN to every Database.session would introduce nested-transaction conflicts. A new shared helper must therefore be opt-in, with one explicit owner.

**Unverified assertions:** comments cite OOM for large BEGIN plus upsert workloads. This is a reason for a memory gate, not measured proof that bounded DELETE plus INSERT cannot work. The lost prototype cannot be used as implementation evidence. The handoff's reported post-COMMIT inconsistencies remain an adverse recovery risk; this small exception diagnostic neither reproduces nor disproves them. They are why recovery, indexed-read agreement and retry gates remain mandatory for A. The suspected DuckDB engine defect remains unconfirmed.

## 4. Comparison of three bounded options

| Option | Verified support | Hypothesis still to prove | Migration | Complexity |
| --- | --- | --- | --- | --- |
| A. One bounded transaction per issuer | One missing boundary explains the reproduced defect; small explicit-transaction save already exists; transaction patterns exist elsewhere in the repo | Realistic replacement fits memory; constraints, readers, interrupted COMMIT and recovery behave correctly | No new SEC schema or file format required | Medium |
| B. Compatible engine configuration/version plus atomic writer | Current lock identifies the runtime; no alternate runtime was qualified here | A specific supported configuration/version fixes relevant behavior without regressions | Same-version settings may need none; version changes may require logical migration and rebuilt indexes | Medium, potentially Large |
| C. Versioned snapshot publication | Would separate large staging work from small publication and preserve previous rows by construction | Complete reader coverage, publication recovery and index consistency still require proof | Generation-scoped SEC storage, keys, publication metadata, legacy certification and cleanup policy | Large relative to this blocker |

B alone cannot fix autocommitted partial publication. No engine change or checkpoint switch is proven safe by this task. C can lower transaction-memory pressure, but introduces a second logical snapshot model and changes all readers, direct SQL, provenance and migration behavior. Keeping generations in one DuckDB file also does not establish engine-corruption immunity.

A has the smallest code/data-model surface that directly addresses the verified defect. The evidence does not currently justify paying C's migration and reader complexity.

## 5. One recommended solution

**Prefer A: normalize and validate first, then replace one issuer's complete SEC snapshot in one explicit, memory-bounded transaction on the existing engine and schema.**

All issuer/security/filing/fact changes and the ingestion run's success status/counts commit together. Ordinary failures roll back to the previous complete state; failure bookkeeping follows on a clean connection. A reader's logical calculation uses one consistent read transaction so it cannot combine statements across a concurrent publication.

This is a preferred design whose release safety remains to be demonstrated. A bare BEGIN/COMMIT wrapper is insufficient: it must include status publication, failure handling, memory controls and reader ownership.

Do not implement C in parallel or change DuckDB preemptively. If A fails one of the bounded gates below, stop with that concrete failure and remain BLOCKED. Reconsider scope through review; do not start an open-ended configuration search.

## 6. Required code and migration scope

1. **Ingestion orchestration:** one transaction owner; prepare before BEGIN; validate single-CIK membership, IDs and replacement expectations. Keep network fetches outside the transaction. Move successful run finalization into the data transaction. Roll back before recording a normal failure on a fresh connection, and preserve the original error if recording also fails.
2. **Ambiguous outcomes:** treat a COMMIT exception or lost acknowledgement as unknown, not automatically rolled back. Reopen and reconcile the unique run ID: success in the same transaction identifies committed data. Never overwrite a committed success as failed. An interrupted pre-commit run may remain running until the next exclusive writer reconciles it to failed/interrupted. An unavailable database cannot guarantee a durable failure record; surface that condition explicitly.
3. **Bounded writes:** change facts/filings row conversion to build only an insertion batch at a time. Keep DELETE plus INSERT for the single issuer; do not introduce a full-payload ON CONFLICT operation. Define a realistic supported input envelope and reject oversize or unexpected empty replacements before BEGIN. Enforce input/normalization size limits if measurements show the existing fully materialized JSON/lists exceed the budget; chunking SQL alone does not bound total process memory.
4. **Complete snapshot semantics:** make the target CIK explicit, including zero-row cases; reject ambiguous empty SEC payloads rather than silently retaining stale subsets. Reconcile security membership as part of the same transaction if the submitted snapshot is authoritative. Preserve parent keys and respect foreign-key ordering; test existing-issuer metadata changes and removed securities.
5. **Transaction helpers/readers:** optional explicit write/read contexts, not a global session behavior change. Pin one read context across metadata, facts, financial periods, shares and factor calculation. Reuse an already-owned context instead of nesting BEGIN. End a read phase before calling a repository that owns its own persistence transaction, or pass ownership explicitly. Audit CLI/research/portfolio entry points and direct FinancialsService/FactorEngine use.
6. **Tests/docs:** retain the diagnostic as evidence and add production regressions separately after review. Replace the failure test that accepts partial persistence. Document replacement behavior, memory limits, unknown outcomes and recovery.

**Migration:** no new generation columns/tables, no engine downgrade, no physical database conversion. Existing schema/migration IDs and financial IDs remain intact. Existing corrupt or partially replaced databases are not repaired by future transaction discipline. Document validation and complete re-ingestion before trusting affected legacy data; do not certify it from a historical success flag alone. Preserve pre-upgrade backups. Older derived research remains historical output and must be recomputed if its inputs were affected.

Likely core changes: ingestion.py, database.py, facts.py and filings.py; security reconciliation where needed; narrow transaction ownership changes in financial/factor services and their CLI/research callers. Reader integration makes the effort **Medium**, rather than a two-line fix. Market accounting, selectors, factor formulas and provider semantics remain outside scope.

## 7. Required correctness, crash and memory tests

These are acceptance requirements for a later implementation, not work executed in this decision task.

- **Normal replacement and retention:** seed a complete old snapshot and a second issuer; replace with changed and identical snapshots. Compare canonical content, all four entity sets, statuses and provenance. Ordinary failure before BEGIN or during any write must leave the old snapshot byte-equivalent in domain content. Previous state need not remain as an archive after a successful replacement.
- **Preparation:** fetch/normalization exceptions, mixed CIKs, duplicate/conflicting IDs, nonfinite facts, invalid/empty payloads and oversize inputs. Fail before destructive writes; record the failed run when persistence is available.
- **Publication boundary:** exception before COMMIT, acknowledged COMMIT, and simulated lost acknowledgement. Check coherent old/new visibility from a second connection and consistent multi-statement research reads. Reconciliation must never turn a committed success into failure.
- **Interruption/recovery:** after separate approval, run one finite deterministic matrix on disposable files: process interruption during replacement, during COMMIT, immediately after acknowledgement, and before ordinary close. Reopen in a fresh process, then exercise a recovery-only open/close and another reopen. Before acknowledgement, complete old or complete new is allowed for an unknown outcome; after acknowledged success, complete new is required. No partial snapshot may be admitted as trusted data. Test first ingestion as well as replacement. No termination tests were run here.
- **Retries/indexes:** after each qualifying recovery case, perform one identical and one changed retry. Compare full canonical row sets and relevant selective queries, not counts alone. Capture EXPLAIN plans to prove an indexed path was actually exercised; compare with an independent sequential-scan oracle. Include PK/unique constraints and secondary-index deletion behavior.
- **Cross-issuer isolation:** changes and failures for A leave issuer B, ticker mappings and B's research outputs unchanged. Exercise all supported concurrency modes; unsupported writers must fail before modification.
- **PIT/provenance:** preserve fact IDs, source URLs, accession numbers, filing dates, acceptance times, available_at and units exactly. Include just-before/at/after availability cutoffs, amendments, comparative debt and split-safe share selection. Ingestion time must not become financial availability. Test factors/ranking/portfolio leakage through actual readers.
- **Memory:** measure process peak RSS, normalization, inserts, rollback, repeated ingestion, database/WAL growth and latency with realistic synthetic text widths, multiple concepts/accessions and 25,000 then 100,000 facts plus representative filings. Proposed review budget: a 4-GiB host, at most 1 GiB process peak RSS for the agreed supported envelope. Confirm or revise that numeric budget before implementation qualification. Include validation overhead and pre-existing old data. If a realistic complete issuer cannot fit, A fails qualification; do not silently truncate history or commit smaller partial snapshots.
- **Migration compatibility:** create representative supported legacy schemas in disposable files; initialize, read, replace, fail and reopen with the unchanged locked engine. Verify existing markets, factors, portfolio data and all values/timestamps remain intact. Exercise restartability of existing migrations. No production database is needed.
- **CI/package:** on the exact reviewed implementation SHA run lock verification, locked dependency synchronization, Ruff lint/format, strict mypy, full offline pytest, the repository's three determinism seeds, wheel/sdist isolated installs and package smoke checks. Preserve 1.0.0 and skfolio 1.2.3. Locked-runtime success alone does not certify every version admitted by duckdb>=1.1.0; verify the supported range or obtain separate approval to align metadata. No dependency edit is authorized here.

The previous 18/18 normal-exit trials and 694-test audit cannot replace these gates. No new crash or memory result is claimed.

## 8. Complexity

**Medium.** The data model stays intact and migrations are avoided. Memory-bounded row preparation, run-status reconciliation and explicit reader transaction ownership require careful work and focused integration tests. Engine debugging is not included in this estimate.

## 9. Exact implementation boundaries and non-goals

One issuer transaction, bounded preparation/writes, coherent reader contexts and correct status/error reconciliation. Use existing DuckDB, schema and financial models.

No generation architecture, extra database, persistence framework, automatic repair tool, background cleanup, generic transaction rewrite, provider requests, formula/mapping changes, or vintage-history feature. No resurrection of the old prototype or historical crash evidence. No changes to the existing PRs, release branches, dependencies or package versions. No commits, pushes, PR creation, merge, tag or publication occurred.

Only new diagnostic/test evidence in the detached temporary worktree and this decision document were written. Existing production files remain unchanged.

## 10. Release status and next action

**BLOCKED.** The controlled-exception defect is independently demonstrated. No solution has yet met the crash, reader-consistency and memory gates. The suspected engine defect is unconfirmed.

**One next action:** independent ChatGPT review of this bounded transactional design and its new evidence manifest, specifically transaction ownership, unknown-COMMIT handling and the proposed memory envelope, before authorizing implementation.
