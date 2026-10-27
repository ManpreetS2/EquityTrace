> Historical report preserved for handoff. Its recommendations, counts and local paths describe the reviewed state at that time. Current status is in [DEVELOPMENT_HANDOFF](../DEVELOPMENT_HANDOFF.md). Transactional ingestion and the reconciliation guard are now implemented; crash and resource qualification remain pending.

# EQUITYTRACE — PR #35 CRASH-GATE READINESS

**BLOCKED — HARNESS OR PRODUCT DEFECT REQUIRES CORRECTION**

The harness now checks complete publication state and actual index-first reads. Its non-destructive controls pass. Production reconciliation still trusts a single status view and does not detect contradictory reads. That safety gap must be corrected and reviewed before crash-test authorization. No process termination was executed.

## 1. Reviewed state and files

Reviewed production SHA: a9a1a93a27ff3c9c24cf576aa8b9d184b27adb5e, branch fix/v1-atomic-sec-ingestion, draft PR #35 targeting audit/v1-independent-correctness. HEAD and the remote PR were not changed during this preparation. No commit, push, merge or release occurred.

Inspected scripts/qualify_sec_recovery.py, scripts/sec_ingestion_synthetic.py, src/equitytrace/repositories/ingestion.py, src/equitytrace/transactions.py, src/equitytrace/repositories/facts.py, src/equitytrace/sec/validation.py, src/equitytrace/database.py, issuer/security/filing repositories, tests/test_atomic_ingestion.py, tests/test_set_based_facts.py, the complete GitHub PR diff and the audit reports. The historical failed-memory audit is distinguished from the final set-based qualification report.

Only harness/test/documentation files changed locally:

- scripts/qualify_sec_recovery.py: phase protocol, six-case controller, guarded modes, first-read verification and retry orchestration.
- scripts/sec_recovery_checks.py: synthetic variants, complete-state hashes, actual-query profiles and independent scan oracle.
- tests/test_sec_recovery_harness.py: 17 non-destructive controls, including a diagnostic demonstration of the production gap.
- docs/audits/v1-pr35-crash-gate-readiness.md: this report.

Production persistence, reader code, schema, dependencies and limits are unchanged. Version remains 1.0.0, skfolio 1.2.3 and locked DuckDB 1.5.5.

The preceding review bundle was SHA-256 verified and copied as prior-qualified-evidence.zip. It preserves the original diagnostic patch, manifests, 392 earlier observations, 995 insertion measurements and qualified implementation evidence. No old experiment was reconstructed or rerun.

## 2. Corrected recovery coverage

Each case starts from a new synthetic database with a complete 2,501-fact issuer and a distinct 17-fact unrelated issuer. Replacement changes issuer metadata, financial values/IDs, security mappings, filing document metadata and filing/fact availability timestamps. Historical share class SYNTH.A remains present when SYNTH.B is reported.

The expected old and new states are captured before publication from separate committed-old and transaction-local-new views. The actual unique candidate run ID and complete run row are included. Expected state is a persistence-recovery oracle, not an independent accounting-formula oracle.

All columns of issuers, securities, filings, financial_facts and ingestion_runs are hashed in stable key order. This includes all 21 fact fields, exact float representations, nullable values, UTC-normalized instants and persistence timestamps. Column names and row counts are part of every digest.

Verification covers the run ID/status/counts, issuer metadata, ticker/CIK mappings, retained/new classes, filing accessions and availability, fact IDs/provenance, unrelated issuer data and runs, and PIT boundaries. A matching fact digest alone cannot certify a case. A mixed old/new state, status/count mismatch, changed unrelated issuer or discrepant query view fails before reconciliation/retry.

Before any evidence-changing reconciliation, the verifier saves query profiles, indexed hashes, scan-derived hashes and the complete observed state. Successful recovery then permits reconciliation, an identical retry, a changed retry and another indexed/full-state comparison. Timeout/error stops the matrix; no automatic crash-case retry loop exists.

## 3. Boundary classification

| Arm | Evidence and allowed recovered state |
| --- | --- |
| before_commit | Worker signals ready and remains held before invoking native COMMIT. No pending-call marker is allowed. Only complete old data with the separately persisted running candidate record is valid. |
| pre_or_during_commit | A marker precedes the native call. This is an ambiguous call-adjacent boundary, never proof of interruption inside COMMIT. Without a durable acknowledgement, complete old or new state is allowed. It also cannot exclude a return immediately before the acknowledgement marker was persisted. |
| after_ack | The worker writes an acknowledgement only after conn.execute("COMMIT") returns, then writes ready and remains held. Only complete new data with the successful run/counts is valid. |
| acknowledgement race | If acknowledgement exists after termination in the ambiguous arm, new state is required, but that arm's coverage is false. The finite matrix reports INCONCLUSIVE rather than claiming an in-COMMIT test passed. |

Markers use atomic file replacement, fsync, case/run identifiers, worker PID and increasing event ordinals. Ordinals avoid assuming sub-millisecond monotonic-clock resolution on Windows. A post-ack ready marker must follow acknowledgement. The controller inspects markers after process exit, closing the old acknowledgement-before-kill race.

Polling sleeps only wait for explicit conditions; their duration is not evidence of COMMIT completion. Actual-crash verification additionally requires a matching parent termination receipt and nonzero exit. Ordinary controls explicitly record termination_executed=false and normal_controls_only.

## 4. First-read index verification

Two separate disposable databases are used per arm:

1. Direct fresh-process recovery followed by index-first verification.
2. A fresh process performing only open, SELECT 1 and close; a second fresh process then performs index-first verification.

No initialize, application full scan, reconciliation, retry, index rebuild or explicit checkpoint runs before that first verification query.

The first data SELECT is the complete target-CIK fact lookup. Actual-query JSON profiling is enabled before it; no EXPLAIN ANALYZE pre-read is used. Profiles must identify the intended table and Index Scan. The diagnostic index_scan_max_count=1000000 setting makes the 2,501-row CIK path eligible and is recorded; this is not a proposed production or recovery workaround.

Subsequent probes inspect both CIKs, run primary key, issuer primary key, ticker/class mappings, filing accession, old/new fact IDs and PIT boundaries. Positive mandatory indexed paths must actually use indexes. Optional/empty paths retain their actual profiles; not every PIT predicate is claimed to be an index scan.

The scan oracle reads each table without WHERE and derives each selective result in Python while streaming 500 rows at a time. Full-query profiles must contain no index scan. This avoids assuming that zero index thresholds disable every possible empty primary-key lookup. No production indexes or constraints are removed.

In all six normal-exit controls, the first query used Index Scan and returned 2,501 rows; the later fact-table Sequential Scan returned 2,518. Both open sequences passed on separate databases.

## 5. Non-destructive validation

- 17 new harness tests passed.
- Combined harness, atomic-ingestion, database and set-based tests: 71 passed in 59.04 seconds.
- Six fresh-process normal-exit matrix controls passed, including identical/changed retries.
- The normal matrix ran with the parent's Popen.kill method patched to raise if invoked; recorded kill calls: zero. Its verdict is normal_controls_only, not crash safety.
- Those six controls precede the final evidence-write ordering change; final targeted tests cover saving both views before a disagreement fails. The successful query/commit paths did not change.
- A caught, in-process pre-COMMIT Python unwind preserved the complete old state and running candidate row; subsequent healthy reconciliation/retry passed. No OS signal or process interruption was used.
- Controlled corruption of issuer, security, filing, fact provenance, candidate status and candidate counts was rejected.
- Missing/incorrectly ordered acknowledgement evidence, ambiguous acknowledgement races, indexed/scan disagreement, a non-indexed required path and child-wait timeout behavior are tested.
- Ruff check and format-check pass: 144 files. Mypy passes on the unchanged 75 production source files. Lock check and diff whitespace check pass.

Existing five-job CI with 740 tests applies to reviewed commit a9a1a93. It does not cover these uncommitted harness changes. A full 740-test rerun, new package build or new GitHub CI run was unnecessary for production-unchanged harness preparation and is not claimed.

## 6. Production reconciliation defect and minimal correction

Healthy behavior remains covered: an actual committed success is not demoted by the guarded UPDATE, and an uninspectable run raises IngestionOutcomeUnknown. That does not cover contradictory reads.

Current reconcile_run() first executes UPDATE ... WHERE run_id=? AND status='running', then trusts one SELECT status ... WHERE run_id=?. It neither compares a scan-derived row nor validates associated publication counts/content. reconcile_interrupted_runs() blindly updates every running row and makes no consistency check.

A non-destructive fault-injection test leaves an actual stored run at success but supplies failed to the selective status SELECT. reconcile_run() returns failed. The full stored row remains success. This proves the missing defense under the required contradictory-read condition; it does not reproduce an engine fault or prove a healthy committed row is physically demoted.

Required correction, not implemented:

1. Under existing exclusive ingestion ownership, inspect the candidate by selective lookup and by a streaming unfiltered run-table scan before any status mutation.
2. Compare relevant issuer/security/filing/fact selective views with scan-derived views in one explicit snapshot, preserving caller transaction ownership. A successful run must agree with its recorded counts.
3. Any disagreement, missing ambiguous record or inspection error must raise IngestionOutcomeUnknown, retain the original cause and leave status/data untouched. Do not classify a contradictory successful publication as failed.
4. Only a consistently observed still-running candidate may be conditionally marked interrupted/failed. Validate the conditional update result and subsequent status agreement.
5. Replace the blind bulk reconciliation with enumeration and the same guarded per-run path. Do not let automatic pre-ingestion reconciliation bypass the guard.

After that correction, replace the diagnostic expectation of failed with an IngestionOutcomeUnknown assertion, and test that neither method changes any run or issuer data on disagreement. Retain the healthy success/running and lost-acknowledgement regressions.

The existing read-transaction helper ensures a coherent transaction snapshot; it does not establish indexed/full-scan agreement. No correction was made to either production reconciliation method in this pass.

## 7. Accepted versus qualified memory envelope

Current admission limits are 250,000 facts, 50,000 filings and 128 MiB of traversed raw text. They are not a qualified memory envelope.

Historical qualification covers one realistic 25,000-fact / 125-filing / 256-concept profile, including repeats and rollback, at 651.47 MiB peak RSS on a roughly 63-GiB Windows host. Neither 100,000/250,000 facts nor a 4-GiB host is qualified.

Fixture-size inspection in this pass, without another memory benchmark:

| Metric | Qualified fixture size |
| --- | ---: |
| Compact raw JSON bytes | 4,171,359 |
| Traversed raw string/key bytes | 2,511,840 |
| Expanded typed fact-string bytes | 15,189,220 |
| Filing string bytes | 14,390 |
| Longest individual fact string | 395 bytes |

A row-count limit alone is insufficient: a large concept description can be repeated across many normalized facts while appearing only once in raw input.

Smallest defensible proposal: declare support no broader than the measured profile, and make a candidate explicit envelope of at most 25,000 facts, 125 filings and 256 concept/unit groups, with raw JSON <=4 MiB, traversed text <=3 MiB, expanded fact text <=16 MiB, filing text <=16 KiB and individual fact fields <=512 UTF-8 bytes. For qualification, also bound reported plus retained securities at 32 rows and issuer/security text at 16 KiB. These are proposed admission/qualification bounds, not newly proven guarantees or implemented limits. An issuer whose complete history exceeds the proposed envelope is unsupported; none of that history may be truncated to pass it.

Qualify that candidate with two finite adversarial profiles at those bounds: one emphasizing repeated long concept text and one emphasizing group/metadata diversity. Each should use a fresh process, complete initial/identical/changed/rollback/retry, and a cooperative 1-GiB RSS ceiling on an independently limited 4-GiB environment if making that host claim. Include existing stored history and cross-issuer indexes. Stop on a failed gate; do not expand into an optimization search.

Reject an unsupported complete ingestion before mutation; never truncate history, discard provenance or delete historical securities to fit a limit. Payload parsing before validation is a separate unbounded cost that must be covered by any supported raw-body contract. No production limit or provider behavior changed here.

## 8. Later finite commands — NOT AUTHORIZED NOW

Prerequisites: independently review the harness; implement/review the minimal reconciliation correction under separate authorization; record the exact resulting source SHA/harness hashes; and obtain explicit permission for the six termination cases. Larger-input capacity remains a separate release gate.

From C:/Users/sh1ft/EquityTrace-atomic-sec:

    uv run python scripts/qualify_sec_recovery.py --plan

A fresh normal-only control, if needed after future code changes:

    uv run python scripts/qualify_sec_recovery.py --self-check --output C:/Users/sh1ft/AppData/Local/Temp/equitytrace-recovery-controls-APPROVED-ID

Only after those prerequisites and explicit termination authorization:

    uv run python scripts/qualify_sec_recovery.py --execute-authorized --output C:/Users/sh1ft/AppData/Local/Temp/equitytrace-recovery-crash-APPROVED-ID

Every output path must be new. One authorized invocation schedules exactly six case-specific databases and at most six worker terminations. It stops on a failed observation and never auto-reruns a timing race. An acknowledgement race can leave the matrix INCONCLUSIVE; no inside-native-COMMIT claim is made.

Parent waits and worker leases are bounded. Wait timeouts do not automatically kill a verifier or worker; they stop the matrix and preserve the PID/logs for operator handling. A native database hang is not silently converted into a passing result.

## 9. Remaining blockers and next action

1. Missing production contradictory-read reconciliation guard.
2. Independently reviewed, separately authorized crash qualification still outstanding.
3. Supported capacity beyond the measured profile, and a real constrained-host memory claim, remain unqualified.

The harness and its evidence are preserved as an uncommitted preparation patch. No production code, dependency PR, merge or release was changed.

**Next action:** independently review and authorize only the narrowly bounded production reconciliation correction. Crash-test authorization should follow that correction and harness review.

**BLOCKED — HARNESS OR PRODUCT DEFECT REQUIRES CORRECTION**
