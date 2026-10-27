# Development handoff

## Active state and release boundaries

Repository: https://github.com/ManpreetS2/EquityTrace

Continue on `fix/v1-atomic-sec-ingestion`, draft [PR #35](https://github.com/ManpreetS2/EquityTrace/pull/35), based on `audit/v1-independent-correctness` ([PR #34](https://github.com/ManpreetS2/EquityTrace/pull/34)). Do not merge or rebase this stack during handoff. This documentation commit follows verified implementation head `e1e88c706e5020e43d424b0440eddbb0e645a3b0`; use `git rev-parse HEAD` and GitHub checks for the final handoff commit, rather than a self-referential SHA in this document.

Verified parent head: `0f48c0a12fc10665dc8f78f840084c7765728f45`.
Planning branch: `docs/project-master-plans-2026-10-26`, head `5d213a8e4ece9a53638f3c6106e7935fa8475f0c`, draft [PR #36](https://github.com/ManpreetS2/EquityTrace/pull/36). Its `PROJECT.md`, `docs/plans/EQUITYTRACE_MASTER_PLAN.md` and `docs/plans/QUANT_PLATFORM_ARCHITECTURE_PLAN.md` are pushed there, not merged into this branch. Remote blob hashes were checked against local Git objects. Do not overwrite that branch or assume its plans are on main.

Package version remains **1.0.0**, skfolio **1.2.3**, locked DuckDB **1.5.5**. Main is older than the candidate; cloning main alone does not recover this work.

## Resume on Windows (PowerShell)

Install Git and uv if absent, then open a fresh terminal:

```powershell
winget install --id Git.Git --exact
winget install --id astral-sh.uv --exact
```

```powershell
git clone https://github.com/ManpreetS2/EquityTrace.git
Set-Location EquityTrace
git fetch origin
git switch --track origin/fix/v1-atomic-sec-ingestion
git status --short
git rev-parse HEAD
uv python install 3.12
uv lock --check
uv sync --all-groups --no-editable --locked
Copy-Item .env.example .env
notepad .env
```

Set `EQUITYTRACE_SEC_EMAIL` privately in this local `.env` before live SEC access; configure provider keys only if needed. Never paste credentials into reports or commit `.env`. Each worktree has its own configuration lookup; the old contact setting was in the main checkout, not the implementation checkout. Recreate it locally rather than copying it into Git. Verify without printing the contact or User-Agent:

```powershell
uv run python -c "from equitytrace.config import Settings; Settings().require_sec_email(); print('SEC identification configured')"
git check-ignore .env
uv run equitytrace --help
```

Review plans without changing either branch:

```powershell
git worktree add --detach ..\EquityTrace-plans origin/docs/project-master-plans-2026-10-26
```

## Verification

Offline tests use fixtures/mock transports; no live SEC requests are needed. Run from the implementation checkout:

```powershell
uv lock --check
uv sync --all-groups --no-editable --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build
uv run --no-project python scripts/package_smoke.py
git diff --check
```

The exact deterministic selections and seeds 0, 1 and 42 are maintained in `.github/workflows/ci.yml`; all five GitHub jobs must pass on the actual pushed SHA. The prior implementation head passed 791 tests and all five jobs. Those results alone do not qualify crash recovery or resource capacity.

The recovery harness is committed in `scripts/qualify_sec_recovery.py` and `scripts/sec_recovery_checks.py`, with tests in `tests/test_sec_recovery_harness.py`. Its default/plan mode does not terminate processes:

```powershell
uv run python scripts/qualify_sec_recovery.py --plan
uv run pytest tests/test_sec_recovery_harness.py tests/test_atomic_ingestion.py tests/test_set_based_facts.py
```

Do not execute `--execute-authorized`, terminate processes, run crash tests, rerun capacity matrices or fetch providers merely to verify this handoff. Non-destructive harness controls and previous measurements are already documented.

## Work preserved on GitHub

Atomic single-issuer replacement, bounded typed UNNEST fact batches, guarded uncertain-COMMIT reconciliation, reader snapshots, provenance/PIT safeguards, regressions, recovery preparation and resource qualifier scripts are already committed. See `docs/PROJECT_MAP.md`, `docs/audits/v1-finishing-sprint.md` and the earlier implementation audits.

The three historical decision/readiness reports added during handoff have explicit superseded notices. They retain their historical evidence and must not be read as current implementation status. [SEC geometry inventory](audits/v1-sec-geometry-inventory.md) and its response metadata are safe summaries; raw responses are not on GitHub.

## Private evidence transfer (required separately)

A clone does not include raw SEC evidence, measurements, manifests, ZIP bundles, synthetic databases or credentials. A verified private backup was prepared on the source laptop under `%USERPROFILE%\Documents\EquityTrace-handoff-20261027`. Its `TRANSFER_MANIFEST.json` records original paths, relative transfer paths, sizes and SHA-256 hashes for 2,157 evidence files (1,141,953,088 bytes). Original untracked reports and the pre-handoff tracked diff are also preserved there. Existing temporary originals were not deleted.

Transfer the entire folder through encrypted removable storage or an approved private transfer channel. Do not attach it to GitHub issues, PRs, releases or commits. Keep it outside the clone, for example `%USERPROFILE%\Documents\EquityTrace-evidence`. Recreate `.env` privately; credentials were excluded from this transfer inventory.

Evidence groups under `evidence/`:

- `equitytrace-sec-corpus-20261026`: all previous incomplete attempts plus `acquisition-20261027T035618Z`, 97 raw official responses, complete geometry/source manifests, hashes and external diagnostic scripts. Raw `.wire` files may be gzip encoded; retain exact bytes. The acquisition script contains old absolute paths and is evidence, not a portable production fetch command.
- `equitytrace-v1-finishing-sprint-20261026`: three finite baseline/large-text/diverse memory profiles, manifests, boundary measurements, plans and review bundle.
- `equitytrace-memory-attribution-20261026`: original implementation patch and 392 diagnostic measurements.
- `equitytrace-set-based-qualification-20261026`: bounded insertion qualification and measurements.
- `equitytrace-crash-gate-preparation-20261026`: preserved preparation patches, normal controls and review bundles; no crash certification.
- `equitytrace-reconciliation-correction-20261026`: reconciliation correction evidence and verification.
- `equitytrace-atomic-memory-25000`: original failed-memory evidence.
- `decision-evidence`: original small synthetic exception demonstration. Its diagnostic test source is separately listed in the private transfer inventory when available.

After transfer, verify every listed file before using it:

```powershell
$evidenceRoot = Join-Path $env:USERPROFILE 'Documents\EquityTrace-evidence'
$manifest = Get-Content -LiteralPath (Join-Path $evidenceRoot 'TRANSFER_MANIFEST.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest.files) {
    $file = Join-Path $evidenceRoot $entry.path
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
        throw "Evidence hash mismatch: $($entry.path)"
    }
}
```

Do not automatically open old databases as trustworthy research data. Existing affected historical databases need validation/re-ingestion and derived-result recomputation; this work does not certify or repair them.

## Remaining blockers and next decision

Release remains BLOCKED. Crash recovery is prepared but unqualified and requires separate explicit authorization. The full accepted ingestion range is not resource-qualified; a 25,000-fact synthetic pass is not a 4-GiB-host guarantee. The large-text profile exceeded the provisional 1-GiB target. Real corpus geometry now shows representative capacity exclusions, notably JPM's 169,637 filings versus the current 50,000 limit. XOM's current official identity has sparse history; continuity needs independent review.

Next action: independent review of the real SEC geometry and identity/history coverage, then approve one finite joint-dimension capacity qualification. Do not impose arbitrary limits, truncate history, change dependencies, implement UI, merge, tag or release as part of laptop migration.
