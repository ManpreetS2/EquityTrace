# Independent v1.0 correctness audit

Audit date: 2026-10-24 America/Los_Angeles (execution continued on 2026-10-25 UTC).
Repository: ManpreetS2/EquityTrace. Target: draft PR #33.
Verified remote head before work and again during verification:
`f748fe8452400a532a7d2aae1b5c853ee9b017f5` (`integration/v1.0-rc`).
Work branch: `audit/v1-independent-correctness`, created directly from that head.

No merge, release, tag, force-push, paid provider request, or live SEC/market
validation was performed. No subagents were used.

## Verified

The persistence backend is **DuckDB**, not SQLite. The repository does not have
SQLite migrations to certify. Its DuckDB migration, repository, CLI and reporting
tests are included in the full suite.

Code review and offline checks covered SEC availability selection, raw-price
market capitalization and split reconciliation, factor/ranking propagation,
reference-calendar schedules, portfolio drift and costs, top-N tie provenance,
provider failures, persistence, and report generation.

Existing split reconciliation applies events in `(shares_fact_date, price_date]`,
requires verified interval coverage, rejects conflicting/ineligible events, and
refuses ambiguous share bases when a split falls between fact end and filing.
Raw prices are used for capitalization; weighted-average diluted shares are not
substituted for outstanding shares. Retrospective split coverage is explicitly
labeled as non-vintage evidence. This describes tested offline behavior, not a
claim that a live provider's history is complete.

Existing date parsing honors explicit offsets and interprets date-only cutoffs as
Eastern end-of-day. Availability filters exclude evidence after the cutoff.
Monthly/quarterly tests exercise next-session execution and boundary handling.
Missing held returns fail rather than being filled with zero. Cutoff ties retain
exact top-N selection and persist tie details. These suites were retained and run;
they were not reimplemented.

An independent hand calculation verifies portfolio mechanics: invest 1,000 into
equal A/B weights with 1% gross-turnover costs, leaving 990; apply +20%/-10%, giving
NAV 1,039.50 and weights 4/7, 3/7; switch entirely to B, giving gross turnover 8/7,
cost 11.88 and final NAV **1,027.62**. The engine matches each intermediate value.
This validates the documented weight-return cost convention, not broker fills.

Fresh environment synchronization succeeded. Clean wheel and sdist installations
verify installed imports outside the source tree, metadata, CLI entry points,
CLARABEL configuration and artifact hygiene. The offline research demo completes
through persistence, load, HTML and JSON export and checks repeated writes for
byte identity. Quickstart commands were exercised through locked installation
and the offline demo; credential-dependent examples remain unverified live.

The security branch `origin/fix/v1-security-sec-freshness` was inspected at
`67f31c7` before editing. Its credential-header/cache-refresh commits and changed
file list were reviewed. No security-branch commits were imported, and no
security/provider/client files were edited. The candidate already sends Twelve
Data credentials in the Authorization header; its existing mock-based security
checks are part of the suite.

## Bugs found and fixed

### 1. High: comparative balances replace current debt components

`component_short_term_debt` sorted only by availability. Comparative balances
sharing reporting fy/fp could displace the latest balance date when disclosed
later; equal-time selection also depended on input order.

Reproduction: current short-term borrowing/current LTD are 30 and 7; comparative
values are 10 and 5 and have later availability. A stored snapshot reports **15**
instead of **37**. A separate permutation test reproduces order dependence with
equal timestamps. This can distort total debt, debt-change signals, composite
quality scores, ranks and ultimately portfolio selection.

Fix: prioritize balance date, then availability, accession and fact ID. The two
stored-ingestion permutations and the direct permutation test fail before the fix
and pass afterward.

### 2. High: missing debt is treated as zero

`component_total_debt` accepted either component alone and silently substituted
zero for the other. With a known component of 100 and the other unknown, it
reported a complete total of **100**. A missing current-year long-term debt tag
could create apparent deleveraging against a complete prior-year total.

Fix: require both current and noncurrent components to be present, including an
explicit zero when applicable. Direct total-debt tags remain preferred by the
service. Both missing-side regressions fail before the fix; explicit zero still
works. An end-to-end FactorEngine regression confirms that absent current-year
long-term debt makes debt_change unavailable, retaining the known prior total
of 50 instead of publishing a false favorable signal.

Compatibility change: incomplete coverage now produces missing totals and may
exclude a debt factor; callers must not rely on fabricated partial totals.

### 3. Medium: net debt is published without cash evidence

With total debt 100, investments 20 and missing cash, `compute_derived_values`
reported **80** with no missing-input warning. If actual cash were 30, the correct
number would be 50. Investments alone satisfied the old aggregate-cash guard.

Fix: require cash and equivalents as well as debt. Missing cash stays unavailable;
reported cash zero still yields 80, and reported cash 30 yields 50. The regression
fails before the fix and passes afterward. The existing convention that omitted
short-term investments are optional is unchanged.

### 4. High: nonfinite financial facts pass SEC normalization

SEC-style payloads containing NaN, +Infinity and -Infinity were accepted as
FinancialFact values. They could enter DuckDB and contaminate arithmetic,
comparisons and serialization. All three new normalization regressions initially
failed because no validation error was raised.

Fix: require finite values at the FinancialFact domain boundary. Normalization
now raises a validation error before snapshot persistence; the existing ingestion
exception path records failure. The regression tests exercise actual payload
normalization, not just a duplicate implementation of the numeric check.
Existing databases containing invalid values are not automatically repaired;
re-ingestion from valid evidence or an explicit data repair remains necessary.

## Changes

- `e9e0619`: financial derivation/selection fixes and independent regression checks.
- `335aea0`: finite SEC fact validation and three normalization regressions.
- `4e5198c`: downstream missing-debt FactorEngine regression.

## Commands and outcomes

Run from the repository root using Python 3.12.1 and the selected Codex model.

```text
uv sync --all-groups --locked
  PASS: created .venv and installed the locked environment.

uv run pytest tests/test_independent_correctness.py
  Before derivation fixes: 4 failed (cash and comparative selection cases).
  Added missing-debt cases before their fix: 2 failed, 4 passed.

uv run pytest tests/test_independent_correctness.py
  Before numeric fix, with then-inline numeric cases: 3 failed, 7 passed.
  The numeric cases were subsequently moved to test_independent_numeric.py.

uv run pytest tests/test_independent_correctness.py tests/test_independent_numeric.py
  PASS: 11 passed in 1.80s after the downstream regression was added.

uv run pytest tests/test_independent_correctness.py tests/test_financials.py tests/test_factors.py tests/test_audit_v02.py
  PASS: 49 passed in 3.68s (before the later numeric/downstream additions).

uv run ruff check .
  PASS: All checks passed.
uv run ruff format --check .
  PASS: 131 files already formatted.
uv run mypy src
  PASS: no issues found in 73 source files.
uv lock --check
  PASS: resolved 58 packages; lockfile unchanged.
git diff --check
  PASS.

PYTHONHASHSEED=0 uv run pytest tests/test_independent_correctness.py tests/test_top_n_ties.py tests/test_portfolio_schedules.py tests/test_research_reporting.py
  PASS: 52 passed in 11.20s.
PYTHONHASHSEED=42 uv run pytest tests/test_independent_correctness.py tests/test_top_n_ties.py tests/test_portfolio_schedules.py tests/test_research_reporting.py
  PASS: 52 passed in 11.58s.
  These runs preceded the additional downstream missing-debt test.

uv run --no-project python scripts/package_smoke.py
  PASS: package-smoke ok for wheel and sdist; rerun on final implementation.

uv run python scripts/demo_research.py --output-dir /tmp/equitytrace-independent-final-demo --keep-db
  PASS: success, 2 rebalances, 4 equity points, 4 signal/transition rows.
  Final NAV 1.0226775487500002.
  Fingerprint e99737a26c98cb7b9476f14a590a09cb1d874a27dcab299e9b175222e97b4e14.
  Synthetic-only banner and non-vintage/survivorship warnings retained.

EQUITYTRACE_MARKET_DATA_REQUESTS_PER_MINUTE=600000 uv run pytest
  PASS: 659 passed in 98.55s (0:01:38), no exclusions.
```

The first plain `uv run pytest` run was interrupted after 221 passed in 260.43s,
while waiting in the provider rate limiter; an overlapping verbose run was also
stopped. Neither is counted as a full-suite pass. The final run above increases
only the configured request budget for mock transports to remove needless sleeps;
production defaults are unchanged and dedicated limiter/retry tests still run.
No test exclusions were used.

## Unverified and remaining risks

- No live SEC or Twelve Data calls: provider completeness, vendor-vintage PIT,
  corporate-action corrections, actual multi-class coverage and production
  credential behavior are not certified by fixtures.
- Ingestion intentionally uses non-atomic multi-table DELETE/INSERT operations.
  Existing failure-injection tests show re-ingestion repairs state, but do not
  establish that readers cannot observe partial data or that a process crash
  preserves the previous snapshot. This remains a release reliability concern.
- The incomplete-total guard does not establish that every issuer's current-debt
  subcomponents are complete. In particular, absent current-maturity tags and
  fallback long-term-debt concepts with ambiguous scope warrant a separate
  accounting-mapping review. No claim of universal debt reconciliation is made.
- Large real-company payload memory behavior, abrupt process termination,
  concurrent readers and live credential-dependent quickstart steps were not
  independently load-tested here.
- Modeled bar/split availability, provider-adjusted history and user-selected
  universes retain their documented non-vintage/survivorship limitations.

## Release decision and next action

**PR #33 at f748fe8 is not suitable for integration as-is.** Four reproducible
numeric/data-validation defects were found despite the existing suite. The audit
branch corrects those cases but does not certify the remaining risks above.

Next action: review and incorporate the draft audit PR into `integration/v1.0-rc`,
then reassess release readiness with the non-atomic-ingestion limitation explicit.
No merge or publication is authorized by this report.

## Final debt-consistency hardening on PR #34

Starting/reviewed SHA: `1186d7a8bf58198533cf77037bb17659c425ccf4`.
The local branch and remote PR head both matched it before work; the target
remained `integration/v1.0-rc` at `f748fe8452400a532a7d2aae1b5c853ee9b017f5`.
The worktree was clean. All six original audit files were reviewed; the original
cash/nonfinite fixes remain intact. Additional edits are confined to debt
selection/service, debt regression fixtures/tests and this report.

### Reproduced failure

An FY2023 filing carries ShortTermBorrowings 30 at 2023-12-31,
LongTermDebtCurrent 5 at 2022-12-31, and LongTermDebtNoncurrent 40 at
2023-12-31, without complete direct debt tags. The prior FY2022 direct total
is 100. On the reviewed SHA the snapshot reports current debt **35**, total
debt **75**, and a valid debt_change **-25**. Reversing ingestion order gives the
same incorrect result. Omitting current LTD entirely instead reports 30, 70
and -30; the missing borrowing case similarly publishes partial debt.

The initial focused regressions ran against the functioning pre-fix sources:
`uv run pytest tests/test_debt_consistency.py` produced **26 failed, 4 passed**.
Eight failures show invented complete balances/downstream signals; sixteen show
that valid derived total-debt provenance omitted its source date; two show that
the total-debt helper accepted mismatched or unknown dates. The four valid
complete direct-debt cases already passed. Five additional boundary/scope cases
were then added, for 35 focused cases in the final file.

### Correction and compatibility

Current-debt fallback now requires both borrowing/commercial-paper and current
LTD evidence on the same current balance date. The service anchors that date to
the latest eligible selected balance-sheet evidence for the requested fiscal
period; the helper independently infers it from mapped instant facts when called
without an anchor. Availability cutoffs remain enforced. A missing component
returns an explanatory unavailable CanonicalValue, which replaces the initially
selected partial fallback. Matching prior comparative dates cannot replace a
missing current balance when current balance evidence exists.

Total-debt fallback requires the same known component balance date and a known
noncurrent LTD scope (`LongTermDebtNoncurrent`). Ambiguous fallback concepts are
not assumed to exclude current maturities. Derived total provenance now records
the common period_end, input identities, accessions and maximum availability.
Valid current-date direct DebtCurrent and direct total debt remain authoritative,
including reported zeros; stale comparative direct values cannot bypass the
guards. A strengthened direct-debt regression also makes the partial borrowing
row available one day later than the complete DebtCurrent. It initially exposed
two preservation failures (zero and 37), so the service now explicitly selects
complete current-date DebtCurrent before attempting component fallback. Missing debt propagates to an invalid/unavailable debt_change factor,
with current input None and prior input 100 in the reproduced case.

The existing synthetic ALPHA fixture had relied on omitted current-maturity tags
as zero. It now explicitly reports zero current maturities for its two annual
balances, preserving its intended complete annual totals and factor results.
The older independent unit cases now provide explicit zero/date provenance as
well. No assertions were relaxed to accept partial debt; missing-evidence cases
remain separately tested as unavailable. No concept mappings, dependencies,
schemas, security files or release references changed.

### Final local verification

```text
uv lock --check
  PASS: 58 packages resolved; lockfile unchanged.
uv sync --all-groups --no-editable --locked
  PASS: locked environment synchronized and package installed non-editably.
uv run ruff check .
  PASS.
uv run ruff format --check .
  PASS: 133 files already formatted.
uv run mypy src
  PASS: no issues found in 73 source files.
git diff --check
  PASS.
uv run pytest tests/test_debt_consistency.py tests/test_independent_correctness.py tests/test_independent_numeric.py
  PASS: 46 passed in 4.24s (35 new consistency cases plus 11 original audit cases).
EQUITYTRACE_MARKET_DATA_REQUESTS_PER_MINUTE=600000 uv run pytest
  PASS: 694 passed in 110.04s (0:01:50), no exclusions.
uv run --no-project python scripts/package_smoke.py
  PASS: wheel/sdist build, isolated installs and package/CLI smoke; package-smoke ok.
```

The full suite uses the same mock-only request-budget override as the initial
audit; production defaults and CI remain unchanged. There are no test exclusions
and no live SEC/Twelve Data calls. The exact existing CI determinism selection
was extracted without editing the workflow and run with this command:

```python
import os, shlex, subprocess
from pathlib import Path

selection = shlex.split(Path(".github/workflows/ci.yml").read_text().split("run: >\n")[-1])
for seed in ("0", "1", "42"):
    command = ["uv", "run", "pytest", *selection[3:]]
    env = dict(os.environ, PYTHONHASHSEED=seed, EQUITYTRACE_SEC_EMAIL="ci@example.com")
    subprocess.run(command, env=env, check=True)
```

Seed 0: **18 passed in 12.13s**. Seed 1: **18 passed in 14.85s**.
Seed 42: **18 passed in 13.51s**. Final GitHub CI must also pass all five jobs
on the exact pushed correction SHA; its run and conclusions are recorded in the
final review handoff, without another documentation-only push invalidating that SHA.

### Remaining financial risks

The earlier current-debt-completeness/ambiguous-scope concern is now handled by
returning unavailable rather than assuming zero or noncurrent scope. This can
reduce factor coverage for issuers lacking sufficient XBRL evidence; it is an
intentional compatibility change. Identical fy/fp metadata alone still cannot
prove the actual current balance date when no later balance evidence exists.
Issuer-specific classifications require source evidence, not expanded mappings.
Previously persisted derived results are not rewritten; recompute research runs
after applying this correction. Non-atomic ingestion, provider-vintage limits,
survivorship bias and live-provider completeness remain as described above.
