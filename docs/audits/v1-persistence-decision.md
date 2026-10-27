> Historical report preserved for handoff. Its recommendations, counts and local paths describe the reviewed state at that time. Current status is in [DEVELOPMENT_HANDOFF](../DEVELOPMENT_HANDOFF.md). Transactional ingestion and the reconciliation guard are now implemented; crash and resource qualification remain pending.

# EQUITYTRACE — v1.0 PERSISTENCE DECISION

Date: 2026-10-26. Status: **BLOCKED**. Recommendation: **Option C — snapshot generations**, proposed for independent ChatGPT review only.

## Repository state

The attached workspace is `C:/Users/sh1ft/EquityTrace`, branch `main`, HEAD `c368864f8bb9d4f5fc37c1e2731b95585a07bfd1`. It was clean before this report. It is not the macOS workspace or investigation branch in the handoff. No branch switch, dependency installation, database opening, experiment, production edit, commit, or PR mutation was performed.

The supplied last verified SHA, `0f48c0a12fc10665dc8f78f840084c7765728f45`, exists locally and was inspected using Git object reads. It is the head of the independent accounting audit, not proof that the investigation branch or its uncommitted prototype has been recovered. `fix/v1-sec-ingestion-consistency` is absent from local branches and the remote branch lookup returned no match.

At that candidate SHA, package version is **1.0.0**, skfolio is **1.2.3**, and the lockfile resolves DuckDB **1.5.5**. The package requirement is broader: `duckdb>=1.1.0`. The attached main checkout instead declares **1.0.0.dev0**. All were left unchanged; do not mistake main for the v1 candidate.

Read-only GitHub metadata confirms these open dependencies:

- [PR #19](https://github.com/ManpreetS2/EquityTrace/pull/19): original release candidate, head `c67d865`.
- [PR #30](https://github.com/ManpreetS2/EquityTrace/pull/30): split-safe market capitalization, head `de2432a`.
- [PR #33](https://github.com/ManpreetS2/EquityTrace/pull/33): draft integration candidate, head `f748fe8`; its title identifies integration of #19, #30, #31 and #32.
- [PR #34](https://github.com/ManpreetS2/EquityTrace/pull/34): draft accounting corrections, head `0f48c0a`, targeting #33's integration branch.

No merge order or authorization is changed by this decision.

## Evidence and recovery limits

**Artifact recovery is incomplete.** Neither `v1-sec-ingestion-consistency.md` nor `v1-checkpoint-isolation.md`, recovery logs, SQL traces, manifests, or the rejected transactional prototype were found in this checkout, the candidate tree, the local T3 checkpoint tree, or the bounded local-folder search. Thread search found only the current handoff. Their absence is a provenance gap; this report does not claim to have read them.

Three evidence levels apply:

1. **Directly verified in candidate code:** `Database.session()` closes connections without wrapping the caller's work in a transaction. Ingestion independently upserts issuer/security metadata, replaces filings using DELETE plus chunked INSERT, then replaces facts the same way. Run success/failure is recorded separately. Research queries read the underlying tables without requiring a successful ingestion. An exception after deletion can therefore leave a partial replacement readable.
2. **Established by the supplied investigation handoff, not independently rechecked here:** controlled loss of the previous snapshot; ordinary rollback success in the prototype; post-acknowledged-COMMIT recovery failures; 2,501 facts in a full scan versus zero in a filtered query; duplicate fact-ID failure on retry; index-deletion failure on identical replacement. Environment: Python 3.12.1, DuckDB 1.5.5, macOS arm64.
3. **Independent upstream context:** similar persisted secondary-index divergence is reported upstream, including a later sequential-process report on 1.5.5/Darwin arm64. This supports a hypothesis, not identity of defects.

Taken as preserved findings, the completed investigation establishes that the original writer violates the accepted integrity standard and that the rejected prototype is not release-qualified. A successful rollback test does not establish durable recovery or safe retry. The available evidence does not justify asserting that every COMMIT is unsafe, that base-table rows were physically lost, or that one particular DuckDB component is conclusively responsible.

The committed [independent audit](https://github.com/ManpreetS2/EquityTrace/blob/0f48c0a12fc10665dc8f78f840084c7765728f45/docs/audits/v1-independent-correctness.md) itself leaves non-atomic ingestion and abrupt-termination behavior unresolved. Its 694 passing tests are not a release override.

## 18/18 checkpoint tests

Accept the completed result without repeating it: **18/18 normal-exit checkpoint-isolation trials passed; INCONCLUSIVE — RELEASE STILL BLOCKED.**

They demonstrate that the tested normal-exit cases met their recorded assertions in that environment. Without the report and manifests, their exact configurations, assertions and coverage cannot be independently enumerated here.

They do not prove interruption safety after COMMIT, recovery-only open/close safety, correct indexed reads following recovery, idempotent retry after recovery, bounded peak memory, or correctness on other supported platforms. Normal shutdown can take a different checkpoint path and may repair state that an interrupted writer leaves behind.

## Root cause

**Confirmed application defect:** destructive replacement and its metadata updates lack one publication boundary. Run status is bookkeeping, not a reader visibility barrier. The facts and filings writers also return early for empty lists, so empty replacement semantics need an explicit contract.

**Unresolved engine hypothesis:** WAL replay and checkpointing may leave secondary ART indexes inconsistent with table storage. Alternative contributors include the prototype's statement order, connection/recovery lifecycle and constraint-index interactions; these cannot be distinguished without its traces and implementation.

[DuckDB issue #23788](https://github.com/duckdb/duckdb/issues/23788) initially describes a two-engine trigger and reports older releases unaffected by that reproducer. Later discussion adds a sequential-process manifestation. [PR #23848](https://github.com/duckdb/duckdb/pull/23848) remains draft in the retrieved page; its author questions the original regression coverage, and a later comment reports an explicit-checkpoint passing control. None establishes a verified EquityTrace fix or a released fix to adopt.

DuckDB documents [CHECKPOINT as WAL-to-data-file synchronization](https://duckdb.org/docs/1.5/sql/statements/checkpoint) and [shutdown checkpoint controls](https://duckdb.org/docs/1.5/configuration/pragmas). Those controls are not a crash-safety guarantee. Disabling checkpoints or checkpointing after COMMIT alone leaves interruption and recovery paths to prove.

## Architecture comparison

Estimates below are planning ranges for one engineer after evidence recovery and review, not measured delivery commitments. Shared release verification is additional.

| Dimension | A — bounded transaction | B — alternate DuckDB | C — snapshot generations |
| --- | --- | --- | --- |
| Supporting evidence | Ordinary rollback reportedly passed; minimal reader changes | Upstream reproducer reports 1.3.2 and 1.1.3 unaffected | Current defect follows destructive replacement; isolated generations preserve old rows |
| Missing evidence | Exact prototype, reliable recovery/configuration sequence, memory proof | An EquityTrace-compatible version that passes recovery, migration and performance gates | Publication/recovery proof, complete reader coverage, migration correctness |
| Financial guarantee if qualified | All four SEC entities change together | Only with an atomic writer such as A; version change alone leaves original defect | Readers use one complete validated generation; incomplete staging stays invisible |
| Crash risk now | High: rejected prototype has a known failure | Unknown: another reproducer's passing version is not a known-good app backend | Residual DuckDB risk remains; publication and recovered reads must be qualified |
| Memory/performance | Large transaction may retain changes/index state; chunking alone is insufficient | Must remeasure planner, ingestion and memory behavior | Small staging commits and small publication; extra scans, storage and reader filtering |
| Complexity | Low code surface, high uncertainty; roughly 2–4 days if a narrow correction exists | Low dependency diff, potentially substantial compatibility work; roughly 2–5 days if compatible | Moderate implementation surface, explicit invariant; roughly 5–10 days |
| Migration | Normally no logical schema change | Older engine may need logical export/import into a fresh file | New generation storage, publication metadata, legacy-data policy |
| Required regressions | Atomic visibility, interruption/recovery, retries, RSS | All A gates plus version/storage/API compatibility | Staging invisibility, publication, pinned reads, recovery, retry, provenance, migration |

**A assessment:** potentially the smallest patch, but not presently a defensible release solution. The available record contains a rejected implementation and no proven replacement sequence. A candidate sequence must cover recovery connections that do not query SEC indexes before closing; a happy-path explicit checkpoint is insufficient. No configuration is declared safe here.

**B assessment:** feasible to investigate only after separate approval. DuckDB 1.3.2 is a lead from the upstream issue, not a recommendation or certified version. A downgrade would still require fixing application atomicity. Physical-file backward readability, Python/platform wheels, SQL behavior, TIMESTAMPTZ handling and logical migration must be checked against the actual candidate. Never open the only copy of a 1.5.5 database with an older engine. DuckDB's [storage compatibility documentation](https://duckdb.org/docs/1.5/internals/storage) is the starting point, not evidence that a particular existing file is compatible.

**C assessment:** removes destructive active-snapshot replacement from ingestion and separates payload size from publication transaction size. It changes more code than A, but its application invariant can be tested directly without first proving one engine root cause. Generations in the same database do not provide independent protection from database-wide corruption.

## Recommended solution

Choose **C — append-only staging with atomic generation publication**, subject to independent ChatGPT review and the gates below. This is the smallest defensible design recommendation from the evidence available, not a proven implementation.

For each CIK, a reader must see either the previous complete generation or the new complete generation. Before the first successful publication, it returns unavailable. On recovery inconsistency it must reject unverified data. A previous generation may be served only if independently validated and clearly identified as stale.

Do not build an event-sourcing platform, change storage engines, or pursue several fixes in parallel. If the reviewed C design fails its recovery gate, remain blocked and review that concrete failure; do not silently relax the product standard.

## Required implementation

No implementation is authorized or started by this report.

1. **Schema:** add four generation-scoped SEC storage tables for issuers, securities, filings and facts, plus a generation manifest and append-only publication log. Scope storage keys by generation, retaining original fact IDs/accessions as domain provenance. The manifest records expected counts and deterministic content digests for all four sets, including intentionally empty sets. Separate generation identity from financial availability timestamps.
2. **Writer:** normalize and validate before publication; insert a fresh generation in bounded chunks without deleting active data. Validate stored content against the manifest. Seal staging against further writes, then use one small transaction to insert its publication record and record run success. Serialize writers and assign publication order explicitly; do not choose the latest generation by wall clock or UUID. A retry uses a fresh generation or an explicitly reconciled published run, never a destructive overwrite.
3. **Ambiguous outcome:** a lost COMMIT acknowledgement is an unknown outcome. Reconcile publication by run/generation ID on reopen. Never mark a committed publication failed or delete its data merely because the client did not receive confirmation.
4. **Readers:** expose only published generations, and pin one generation per issuer for a complete calculation/report in a consistent read context. Apply that context to metadata, ticker resolution, filings, facts, counts and research. Audit direct SQL in financial annual-period enumeration, shares selection, market-cap resolution and market repository security counts; changing FactsRepository alone is insufficient.
5. **Recovered-read integrity:** validate manifests on reopening before admitting recovered SEC data to research. Counts alone are insufficient: check canonical content and compare actual reader results with an independent sequential-scan oracle. Initially avoid optional secondary ART indexes on the new SEC tables, and qualify query plans so critical reads cannot silently rely on an unverified recovered index. Primary/unique/foreign-key indexes can remain a risk; their design and recovery behavior still require proof. Do not treat an index bypass or checksum as a general cure for engine corruption.
6. **Migration and provenance:** use a versioned, restartable migration on a preserved database copy. Do not certify legacy rows from a historical success flag or count alone: later failed replacements may already have damaged them. Certify only against trustworthy complete source evidence; otherwise retain legacy data for diagnosis and require re-ingestion before trustworthy research. Record source generation IDs in new factor/research provenance; old derived results remain historical/unverified and require recomputation before use as current trusted results.
7. **Cleanup:** defer automatic deletion/compaction from v1 ingestion. Retain previous complete generations and failed staging; preflight disk space and fail before mutation when capacity is insufficient. Document storage growth. Any later cleanup needs exclusive maintenance, retention checks and its own recovery qualification.

Expected surface: one schema migration, ingestion orchestration, the four SEC repositories and a shared generation context; direct SQL readers, statistics and derived-result provenance. Budget approximately **10–15 production modules plus focused tests/documentation**, subject to recovered-prototype review. No new persistence framework.

Storage grows by roughly one issuer payload per retained generation plus metadata/index overhead. Publication work is small; validation scans and generation joins add latency. Chunking bounds additional insert buffers, not the existing in-memory SEC JSON and normalized lists. Measure those separately and avoid building another full rows list.

## Verification gates

These are future gates after review and specific authorization. None was run here. Reuse completed evidence as baseline; do not rerun the 18 normal-exit trials automatically.

- **Crash consistency:** use one finite, deterministic matrix for the approved implementation: interruption during staging; after staging commits; after validation/before publication; during publication COMMIT; after acknowledged publication COMMIT/before close; and recovery-only open/close followed by another reopen. Check both existing-issuer replacement and first ingestion. Each recovery must expose complete old or complete new content, or fail closed; acknowledged success must recover the new generation. Preserve DB/WAL artifacts and SQL order before diagnostic opens. Include identical retry and a changed second ingestion. Read through every affected research path, compare counts plus exact canonical content, and verify relevant scan/index plans. Require a passing macOS arm64/Python 3.12.1/DuckDB 1.5.5 case; Windows success cannot substitute.
- **Memory/performance:** use archived offline payloads representing large issuers and a constrained host. Measure process peak RSS, ingestion/reopen latency, WAL and database growth, normalization, insert buffers and validation. Set a numeric host budget before testing. DuckDB [memory_limit controls the buffer manager, not total process memory](https://duckdb.org/docs/1.5/configuration/pragmas), so setting it is not sufficient evidence.
- **DuckDB/schema migration:** fresh database and copies of supported legacy schemas; multiple issuers; suspected partial legacy data; interrupted/resumed migration; idempotent restart; preserved original backups and tested restore. Verify data values, timestamps, IDs, constraints and market/portfolio tables. No dependency change is part of C. If the tested DuckDB version is the only supported release version, align published dependency metadata with that tested support claim before release through a separately approved change; a lockfile alone does not constrain wheel installs.
- **PIT integrity:** retain available_at cutoffs and acceptance timestamps exactly; generation publication time must not become financial availability. Test same-accession changes, late filings, amendments, empty payload policy and failed refresh. Check financials, shares, split-safe market cap, debt completeness, factors, ranks and portfolio leakage. A generation is an ingestion snapshot, not vintage provider history.
- **Regression/package:** update existing tests that explicitly accept partial issuer/security persistence. Add seeded-complete-snapshot failure tests, generation visibility and run-status reconciliation, reader pinning under publication, direct-SQL coverage and provenance tests. Then run the full offline suite, determinism checks, Ruff, formatting, strict mypy, diff checks, wheel/sdist isolated-install and package smoke checks on the exact candidate SHA. Preserve version 1.0.0 and skfolio 1.2.3.
- **Exit rule:** one reviewed design, one implementation, one bounded qualifying matrix and the required repository/package checks. Any failure must have a specific correctness explanation or leave the release blocked; do not substitute repeated happy-path trials for the missing guarantee.

## Release status

**BLOCKED.** The original path has an application integrity defect. The transactional prototype has not satisfied recovery safety. The completed checkpoint experiment explicitly remains inconclusive. No proposed replacement has passed its gates, and the original local investigation bundle is unavailable in this workspace.

Non-vintage provider history, survivorship and live-provider coverage are documented product limitations. Exposing a partially replaced issuer as trustworthy research data is an integrity defect and cannot be reclassified as one of those limitations.

## Next action

**Obtain independent ChatGPT review of this proposed Option C publication contract against the original reports, traces, manifests and rejected prototype, recovering that bundle from the macOS investigation workspace first.** Review must resolve recovered-read/index handling and legacy certification before architecture changes or implementation. No new PR, merge, dependency change, live provider run, termination experiment, tag or publication should occur at this stage.

