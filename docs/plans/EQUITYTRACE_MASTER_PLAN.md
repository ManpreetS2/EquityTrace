<!--
Canonical Markdown planning mirror for EquityTrace project governance.
Source: https://docs.google.com/document/d/1nh3J0-ujhv3kTDxw7GqlP7JLMk8NUC5Iv5xv6Hb05v4
Google Docs revision: AHj4eMSSWRGkrQjVMyk4qAMsv4Lm2WdlmmEa_8gbdDDWpbaa71sMIH9r-zhuxqJGNk-onj53_K65SBIkpJ8176BRpnOYH0_QUBuAlQSJvKo
Source modified (UTC): 2026-10-25T06:37:21.809Z
Original text paragraphs: 185
Body text preserved in source order. Heading/list prefixes use Markdown formatting.
Historical milestone/status notes are not current GitHub status claims.
-->
# EquityTrace — Portfolio, Backtesting & Quant Expansion Master Plan

*Repository-grounded roadmap • September 2026*

## Purpose

Extend EquityTrace from point-in-time fundamental research into portfolio construction and backtesting without weakening its strongest guarantees: provenance, as-of correctness, deterministic factor logic, and explainability.

## CURRENT PRODUCT BOUNDARY

EquityTrace currently turns SEC filings and point-in-time market data into transparent financial statements, factors, rankings, and reproducible research. Its current roadmap already separates market data, valuation/momentum/risk, portfolio construction/backtesting, and a later strategy builder. Price prediction and live execution should remain outside the core product unless explicitly introduced as separate experimental modules.

## PRIMARY ADOPTION — SKFOLIO

Usefulness: 10/10 for EquityTrace v0.3c portfolio construction and risk.

GitHub: https://github.com/skfolio/skfolio

Docs: https://skfolio.org/

License: BSD 3-Clause.

Why it fits

- Portfolio optimization, risk management, factor models, cross-validation, stress testing, and scikit-learn-compatible model selection map directly onto EquityTrace v0.3c.

- Useful models include mean-risk optimization, risk budgeting, hierarchical risk parity, nested clusters, Black-Litterman, distributionally robust CVaR, benchmark tracking, and ensembles.

- Supports turnover constraints, transaction costs, weight/group constraints, cardinality constraints, tracking error, multiple risk measures, walk-forward validation, and combinatorial purged cross-validation.

Integration rule

EquityTrace must remain the source of truth for point-in-time data, factor provenance, security/issuer identity, availability timestamps, and research datasets. skfolio should receive prepared point-in-time-safe matrices through a narrow adapter rather than owning EquityTrace’s data model. For v0.3c, keep the adapter intentionally narrow: one transparent minimum-variance MeanRisk path only. HRP, Black-Litterman, CVaR, risk budgeting, cardinality, tracking-error variants, and other optimizer families are post-v1 unless later repository evidence justifies them.

Approved v0.3c package boundary

PR 1 native foundation: `portfolio/models.py`, `calendar.py`, `signals.py`, `returns.py`, `baselines.py`, `audit.py`, `engine.py`, and `metrics.py`.

PR 2 integration boundary: `portfolio/optimizer.py` and `portfolio/skfolio_adapter.py`.

Do not pre-create extra portfolio modules unless repository evidence justifies them.

Required safeguards

- Never let a portfolio optimizer see a factor, price, filing, security membership, or corporate-action state that was unavailable at the simulated decision timestamp.

- Keep transaction costs, turnover, rebalance dates, benchmark data, and universe membership explicit.

- Compare optimized portfolios against simple baselines such as equal weight and inverse volatility.

- Record optimization inputs, constraints, solver status, weights, turnover, and resulting risk metrics for reproducibility.

- Treat optimization failure as a visible research result, not a reason to fabricate weights.

## BACKTEST / LEAKAGE AUDIT INSPIRATION — FREQTRADE

Usefulness: high as a testing/reference source; low as an EquityTrace dependency.

GitHub: https://github.com/freqtrade/freqtrade

Docs: https://www.freqtrade.io/

License: GPL-3.0.

Best ideas to study

- Dry-run-first mindset.

- Dedicated look-ahead analysis and recursive-analysis tooling.

- Backtest diagnostics rather than trusting a single headline return.

- Explicit exchange/data assumptions, strategy configuration, persistence, and repeatable backtests.

EquityTrace adaptation

Build an EquityTrace-specific backtest audit layer that checks:

- point-in-time factor availability

- future filing leakage

- future market-data leakage

- adjusted-price/corporate-action leakage

- rebalance timestamp correctness

- universe/survivorship bias

- stale data and missing-data behavior

- transaction-cost and turnover assumptions

- benchmark alignment

Do not copy Freqtrade implementation code into EquityTrace casually because GPL-3.0 obligations differ from EquityTrace’s MIT license. Prefer independent implementation of the research/testing concepts.

## LATER / SEPARATE EXECUTION LAYER — NAUTILUSTRADER

Usefulness: 6/10 for EquityTrace now; 9/10 later if a separate execution project needs institutional-grade simulation/paper/live infrastructure.

GitHub: https://github.com/nautechsystems/nautilus_trader

Docs: https://nautilustrader.io/docs/

License: LGPL-3.0-only.

What it provides

- Rust-native event-driven trading engine with Python control/strategy layer.

- Shared architecture across deterministic simulation and live execution.

- Multi-asset, multi-venue adapters, order types, market data, execution algorithms, and high-resolution backtesting.

- Existing integrations include traditional and crypto venues/data providers.

Boundary rule

Do not turn EquityTrace into an order-management/execution system. If execution is ever required, keep a clean boundary:

EquityTrace research/signals → exported strategy/signal contract → NautilusTrader simulator/paper/live engine.

This keeps EquityTrace focused on explainable research while allowing a separate trading platform to consume its outputs.

## EXPERIMENTAL PREDICTIVE RESEARCH — KRONOS

Usefulness: 4/10 for EquityTrace core; 9/10 for a separate predictive experiment.

GitHub: https://github.com/shiyu-coder/Kronos

Model hub: https://huggingface.co/NeoQuasar

License: repository code is MIT; verify each model/tokenizer weight license separately before redistribution or product use.

What it is

Kronos is a financial-market foundation model for K-line/OHLCV sequences. It tokenizes candlestick data and uses autoregressive Transformers for probabilistic forecasting. Published model sizes include mini, small, and base variants, with fine-tuning and batch-inference tooling.

EquityTrace rule

Do not make Kronos forecasts part of canonical EquityTrace factors or rankings while EquityTrace’s product claim remains non-predictive. If explored, isolate it under an experimental research namespace and label all outputs as model forecasts with model version, data window, forecast horizon, sampling parameters, and evaluation metrics.

Required evaluation before keeping it

- locked out-of-sample test period

- walk-forward evaluation

- naive persistence and simple momentum baselines

- transaction-cost sensitivity

- calibration/error metrics for forecast distributions

- regime analysis

- no leakage from future-adjusted data or future universe membership

Possible future composition

EquityTrace fundamental signal + optional Kronos price/market forecast → skfolio portfolio construction → independent backtest/reporting.

The predictive lane must remain separable so EquityTrace can still operate as a transparent non-predictive research tool.

## NOT A CORE FIT — HUMMINGBOT

Usefulness: 2/10 for EquityTrace core.

GitHub: https://github.com/hummingbot/hummingbot

Docs: https://hummingbot.org/

Condor AI harness: https://github.com/hummingbot/condor

License: Apache 2.0 for Hummingbot; verify Condor’s repository license separately before copying code.

Why not core

Hummingbot is focused on automated crypto trading, market making, arbitrage, CEX/DEX connectivity, execution, grids, TWAP, liquidity provision, and exchange connectors. That is outside EquityTrace’s SEC/fundamental research identity.

Useful concepts only

- Separate model/agent decisions from deterministic trade execution.

- Encrypt and scope exchange credentials.

- Provide paper-trading and status/monitoring paths before live execution.

- Treat exchange connectors as narrow adapters rather than letting strategies depend on venue-specific details.

## EQUITYTRACE ROADMAP PLACEMENT

v0.3b — Valuation, momentum, and risk

- Keep core factor work native and point-in-time-safe.

- Add the data/risk primitives needed before portfolio optimization.

v0.3c — Portfolio construction and backtesting

- Add skfolio through an adapter.

- Add equal-weight/inverse-volatility baselines.

- Add walk-forward portfolio evaluation.

- Add explicit costs/turnover/constraints.

- Add EquityTrace leakage-audit tooling inspired by Freqtrade’s philosophy.

v1.0 — Strategy builder and research interface

- Strategy definitions should compose transparent EquityTrace factors and portfolio rules.

- Keep execution out of the core v1.0 unless a separate integration contract is intentionally designed.

Post-v1 experimental lane

- Optional Kronos forecasting research.

- Optional export bridge to a separate NautilusTrader-based simulation/execution platform.

- Do not merge crypto bot/exchange complexity into the EquityTrace core repository.

## LICENSE / SOURCE RULES

- skfolio — BSD 3-Clause: suitable as an external dependency; preserve required notices for copied source.

- NautilusTrader — LGPL-3.0-only: prefer library/process boundaries and review obligations before modifying/distributing upstream code.

- Freqtrade — GPL-3.0: study architecture/testing ideas; avoid copying implementation into MIT EquityTrace without deliberate license review.

- Hummingbot — Apache 2.0: permissive but preserve notices/terms if code is reused.

- Kronos — MIT repository code; model artifacts may have separate terms.

## FINAL DECISION

Adopt skfolio for EquityTrace v0.3c, study Freqtrade’s anti-leakage/backtest diagnostics, keep NautilusTrader as a later external execution engine, keep Kronos in a clearly experimental predictive lane, and do not integrate Hummingbot into the EquityTrace core.

## REPOSITORY MAP & ROADMAP GOVERNANCE

The repository map is a maintained operating artifact for EquityTrace. Its canonical in-repo location is `docs/PROJECT_MAP.md`. It exists to let Cursor and reviewers navigate directly to the correct files, data flows, invariants, persistence tables, and tests instead of repeatedly rediscovering the repository.

### Operating rules

Before planning or approving EquityTrace work, check the live GitHub repository state first: current `main`, open PRs, relevant CI, and the current project map. If a roadmap or architecture decision is made that is not already captured in this master plan, update this document before implementation begins. Any PR that adds, removes, renames, or materially changes modules, data flows, persistence tables, CLI entry points, or test ownership should update `docs/PROJECT_MAP.md` in the same PR when practical. The map is a navigation index, not a substitute for code review: if the map and repository disagree, the live repository is authoritative and the map must be corrected. Keep EquityTrace focused through v1.0 on point-in-time explainable research, transparent factors, portfolio construction/backtesting, and the research interface; predictive models and live execution remain separate or experimental lanes as defined elsewhere in this plan.

### Cursor usage rule

Cursor prompts should cite the relevant sections and file paths from `docs/PROJECT_MAP.md` so implementation begins from bounded context. Update the map as the codebase evolves rather than asking Cursor to rescan the entire repository for every milestone

## CURRENT EXECUTION ENTRYPOINT — v0.3.0 RELEASE PREPARATION

### As of 2026-09-11, PR #3 has been squash-merged into main. The current live main SHA is a2901a0ede5a654b78fe03d91f8ec51a5edfec38. v0.3a release-hardening is complete and the active milestone is now packaging and release finalization for v0.3.0.

Next Cursor task

Create a release/v0.3.0 branch from the exact current main, bump the package from 0.3.0.dev0 to 0.3.0 using the existing version mechanism, update truthful release documentation and docs/PROJECT_MAP.md status, run Ruff/format/Mypy/Pytest plus hash-seed determinism and migration checks, build wheel and sdist, smoke-install both from clean temporary environments, verify CLI behavior and repository hygiene, push a release-preparation commit, and open a release PR. Do not change financial logic, begin v0.3b, merge the release PR, create the v0.3.0 tag, or create a GitHub release until independent review is complete.

CURRENT EXECUTION ENTRYPOINT — v0.3b

Supersedes the prior v0.3.0 release-preparation entry. As of 2026-09-12, EquityTrace v0.3.0 is released from main SHA d1e78842c467c5f008d95bf604fd8f11f2142821. The annotated v0.3.0 tag resolves to that commit, the GitHub release is published, and there are no open pull requests. v0.3b is now the only active core milestone.

v0.3b implementation contract

Target the next development line as 0.3.1.dev0. Keep the milestone bounded to valuation, momentum, and risk; do not begin portfolio construction or backtesting.

- Native FCF yield resolves stored point-in-time market cap automatically, while retaining an explicit override for controlled tests/manual research.

- Add P/E, P/S, and P/B from PIT financial snapshots plus raw-close × PIT-shares market cap. P/E and P/S must not silently annualize quarters; until deliberate TTM support exists, non-annual denominators are unavailable. P/B may use the selected balance-sheet period. Nonpositive or missing denominators are unavailable.

- Valuation uses raw prices only. Preserve market-cap provenance with factor results/persistence where practical: provider, price date, availability/fetch timestamps, adjustment mode, shares concept/accession, and warnings.

- Market time-series analytics stay in the market layer rather than being forced into fiscal-period factor persistence. Implement transparent 12-1 momentum, one-year annualized volatility, one-year beta versus an explicit benchmark (default SPY), and one-year max drawdown from provider-adjusted closes.

- Market metrics filter bars by available_at <= as_of, use no future bars, never forward-fill missing return dates for beta, expose observation counts/window dates/provider/adjustment mode, and return unavailable for insufficient or ambiguous history.

- Provider-adjusted history is revision-prone and is not a vendor-vintage PIT archive. Surface that limitation; do not claim vintage-adjusted purity.

- Deterministic ranking is allowed only where direction is meaningful: momentum higher-is-better, volatility lower-is-better, max drawdown higher-is-better when represented as a non-positive return; beta has no default good/bad direction.

- Keep comments, PR bodies, and docs concise and human: explain non-obvious financial/PIT decisions, not obvious code mechanics.

Cursor workflow

Create one feature branch/PR from exact main, read docs/PROJECT_MAP.md first, touch only bounded factor/market/CLI/persistence/docs surfaces needed for v0.3b, add focused offline tests, update docs/PROJECT_MAP.md in the same PR, run Ruff/format/Mypy/Pytest plus hash-seed determinism and migration/build/install checks, push and open a reviewable PR, and stop before merge. Do not weaken tests, add providers, create the portfolio package, or start v0.3c/v1.0.

PR #5 independent review gates

As of 2026-09-12, PR #5 is open at b80e16d49de0176c08bcb8f4b3889623c98ded61 with CI green, but merge is blocked on two correctness fixes. Beta must align asset and benchmark over identical return intervals; joining separately computed returns only by end date can pair a multi-day asset return with a one-day benchmark return when an interior trading date is missing. Align common price dates first, then compute matched-interval returns, and test an interior gap. Valuation period validity must also take precedence over market-input errors: quarterly P/E/P/S must remain annual_period_required even when stored market cap is unavailable. After those fixes, rerun owning tests, full quality gates, hash-seed determinism, push to the same PR, and stop for independent re-review.

PR #5 RE-REVIEW STATUS

As of 2026-09-13, PR #5 GitHub head dd519a78b1e099876e52cec1ec84f88d68ce4cd9 contains both required correctness fixes: beta aligns common price dates before calculating matched-interval returns, and quarterly P/E/P/S preserve annual_period_required when market cap is unavailable. GitHub CI run 34739751614 is green with Ruff, format, Mypy, and 272 tests. Independent re-review clears the merge gate. Next action is to squash-merge PR #5 only; do not begin v0.3c until v0.3.1 release finalization is complete.

V0.3.1 RELEASE PREPARATION STATUS

As of 2026-09-14, PR #5 has been squash-merged and live main is 9b9e7bad16e15641338e91d89cf8b0ab79a09453. v0.3b implementation is complete. The prepared local release commit 668d996a687f6e1d1ca9491cc15285e234193700 was built and tested as 0.3.1, but release/v0.3.1 is not present on GitHub yet. Recreate/rebase the release branch onto the squash-merged main, preserving only the release-version/docs changes, push it, open the v0.3.1 release PR, verify CI, and stop before merge/tag/release. Do not start v0.3c until v0.3.1 is published

V0.3.1 CORRECTNESS MERGE STATUS

As of 2026-09-14, PR #7 (`fix: harden v0.3b correctness edge cases`) was independently reviewed and then squash-merged. Live main is `d7dae71f63a96e39b3d527848ad600180ed90239`. PR #7 CI passed Ruff, format, Mypy, and 313 tests. The open `release/v0.3.1` PR #6 still points at pre-#7 history and is currently non-mergeable, so its next task is to incorporate current main, preserve the v0.3.1 version/docs-only release scope, refresh stale release docs/test-count wording, rerun full release verification, push the updated release branch, and stop for independent final review. Do not tag or publish v0.3.1 and do not begin v0.3c implementation until PR #6 is independently approved and merged.

PR #7 ADVERSARIAL CORRECTNESS GATE

As of 2026-09-14, PR #7 head 1fdc95f4d8c443b49c89a0274765b0d60cc940a2 has passed independent review and GitHub CI run 34895041468 with Ruff, format, Mypy, and 313 tests green. The fixes cover fundamental-vs-market-cap error precedence, non-finite manual market-cap handling, duplicate/case-normalized ranking inputs, case-normalized manual-cap mapping keys, and non-positive 12-1 momentum endpoints. PR #7 is approved for squash merge, but the existing v0.3.1 release PR #6 must remain unmerged until PR #7 lands and release/v0.3.1 is refreshed onto the new main. Do not tag v0.3.1 or begin v0.3c until the refreshed release PR passes review and CI..

PARALLEL NON-CORE HARDENING LANE — CI / PACKAGING

As of 2026-09-14, while release/v0.3.1 is being refreshed and reviewed, a separate non-core branch may harden CI, packaging, and test/release verification without touching financial logic or v0.3c architecture. This lane may modernize GitHub Actions, make permissions/concurrency explicit, add wheel/sdist build-and-clean-install smoke checks, add focused hash-seed determinism checks, migrate deprecated setuptools license metadata, and measure/trim test-runtime overhead only where supported by profiling. It must branch from current main, avoid factors/market/SEC/financial logic, avoid portfolio/skfolio work, open a separate PR, and remain unmerged until v0.3.1 is published so it cannot invalidate the release candidate.

V0.3.1 FINAL REVIEW + CI HARDENING REVIEW STATUS

As of 2026-09-14, refreshed release PR #6 is open and mergeable at head `3a92634bf96247d08aa402425bc468af09ba9275` against main `d7dae71f63a96e39b3d527848ad600180ed90239`; GitHub CI run 34907675947 is green and the branch is release/version/docs-only relative to current main. Parallel non-core PR #8 is open and mergeable at head `17bb9ce503e8c850b8ad4d33c4face67572918f0`; GitHub CI run 34908403448 is green with quality, package-smoke, and focused determinism jobs. Next action is cross-review: Agent Red independently reviews PR #6 for final v0.3.1 release approval, while Agent Blue independently reviews PR #8 but leaves it unmerged until v0.3.1 is published. No v0.3c implementation begins until the release is tagged and published.

PR #8 INDEPENDENT REVIEW STATUS

As of 2026-09-14, Agent Blue independently reviewed PR #8 at head `17bb9ce503e8c850b8ad4d33c4face67572918f0`. The infrastructure-only scope is clean: no production financial/PIT logic changes, official GitHub Actions are pinned to immutable release SHAs, uv is pinned, lockfile checks are strict, package smoke genuinely builds and installs wheel/sdist artifacts, PEP 639 metadata is valid, Dependabot configuration is valid, and focused hash-seed determinism is meaningful. CI run 34908403448 is green with 314 tests in the quality job plus package-smoke and seeds 0/1/42. Only low/non-blocking nits were found. PR #8 remains intentionally unmerged until v0.3.1 is published; afterward it must be refreshed against released main and re-reviewed before merge

V0.3.1 RELEASE APPROVAL + V0.3c ACCOUNTING CONTRACT

As of 2026-09-14, release PR #6 at head `3a92634bf96247d08aa402425bc468af09ba9275` against main `d7dae71f63a96e39b3d527848ad600180ed90239` has passed final independent release review. GitHub CI run 34907675947 is green with Ruff, format, Mypy, and 313 tests; targeted regressions, migrations, wheel/sdist builds, clean installs, version consistency, docs, and hygiene are approved. The next action is to merge PR #6, sync main, verify version 0.3.1, create annotated tag `v0.3.1`, publish the GitHub release, verify the tag resolves exactly to the released main commit, and stop before v0.3c implementation.

The v0.3c accounting contract is also now pre-approved for implementation after the release gate and PR #8 refresh. v0.3c will use a deterministic weight-return research backtester, not a fake share/fill ledger. Adjusted provider history is used as a total-return research path with the standing non-vintage warning. Decisions occur on a reference calendar (default SPY), target weights become effective one reference session later, and current weights drift naturally with realized returns between rebalances. Turnover is gross asset turnover `sum(abs(target_weight - drifted_current_weight))`; transaction cost is charged on pre-cost NAV only when a real target transition occurs. Failed/unavailable rebalances keep the drifted current state with zero transition cost. No quantity/fill price is fabricated. Fundamental selection uses each issuer's latest eligible annual period as of decision time, exact-N selection uses deterministic ticker tie-breaks, and min-valid-names defaults to 1. The skfolio adapter will later use common-date 252-return matrices, MeanRisk minimum variance, CLARABEL, no fallback, and a narrow adapter boundary. No v0.3c code begins until v0.3.1 is published and PR #8 has been refreshed/re-reviewed against released main.

V0.3c ACCOUNTING CONTRACT REVIEW STATUS

As of 2026-09-14, the v0.3c preflight is not yet implementation-ready because the hand-worked accounting example failed to drift portfolio weights between rebalances. In a weight-return backtester, current weights must evolve with each asset's realized adjusted return before the next rebalance; turnover and transaction cost must be computed from those drifted pre-rebalance weights, not from the prior target weights. Agent Blue must correct the toy fixture, define the drift recursion explicitly, and confirm persistence distinguishes target weights from realized/current weights before the v0.3c contract is frozen. No v0.3c code may begin until v0.3.1 is published and this accounting contract is approved.

V0.3.1 RELEASED + NEXT EXECUTION GATE

As of 2026-09-15, EquityTrace v0.3.1 is fully released. PR #6 was squash-merged at commit `f635844413f11e91335e9c411745effa262a3196`. Live `main` is the same commit. Annotated tag `v0.3.1` has tag-object SHA `dc61e51f5b07f9195c1640e9b4f253909df0caf2` and peels to `f635844413f11e91335e9c411745effa262a3196`. The GitHub release is published, non-draft, and non-prerelease. PR #8 remains open at head `17bb9ce503e8c850b8ad4d33c4face67572918f0` and must now be refreshed against released main, preserving package version `0.3.1`, then rerun its quality/package-smoke/determinism checks and stop for final review before merge. The corrected v0.3c accounting contract is now implementation-ready: current weights drift with realized returns between rebalances; turnover uses drifted current weights; failed rebalances preserve that drifted state with zero cost; target and current weights are distinct; no implicit daily rebalancing occurs. v0.3c implementation still waits until PR #8 is refreshed, reviewed, and merged.

.

V0.3c IMPLEMENTATION-READINESS CORRECTION

As of 2026-09-15, the implementation-readiness audit is accepted with two corrections that override the audit report before coding begins. First, the v0.3c backtest state uses float64 for NAV, returns, current weights, cash weight, turnover, and cost arithmetic with an explicit 1e-12 normalization tolerance; do not reintroduce a Decimal-based evolving state, which would create unnecessary Decimal/float conversion paths around adjusted-return math. Configuration fields such as cost-bps/max-weight may remain strictly validated and converted once at the run boundary. Second, the hand-worked toy backtest maximum drawdown is not exactly -5%. The equity curve already reaches 1.04895 at the earlier valuation session before the second rebalance; the later transaction cost does not erase that prior peak. With final NAV 0.99555345, max drawdown is 0.99555345 / 1.04895 - 1 = approximately -0.0509047619. Persist one post-event NAV per valuation session, but compute running peaks across all prior persisted valuation sessions. These corrections must be included in the v0.3c PR 1 implementation prompt and regression tests

PR #8 POST-v0.3.1 FINAL APPROVAL

As of 2026-09-15, PR #8 (`chore: harden CI and package verification`) was squash-merged into `main` at `7b630ea1484eec0a3ffb6df83096bb5393073988`. The released package remains `0.3.1`, and the hardened CI/package checks are now part of the implementation base. v0.3c PR 1 is authorized to branch from this exact main and move to `0.3.2.dev0`, implementing only the native deterministic portfolio/backtest foundation under the approved accounting and PIT contracts. skfolio remains PR 2 through a narrow adapter. Dependabot PR #9 is a separate dependency-maintenance lane and should remain deferred while PR 1 is active unless concrete security evidence requires interruption. The stale pre-v0.3c audit branch is cleanup-only and must not be used as an implementation base.

.

## PROJECT SCOPE DECISION — FINANCIAL MODELING SPLIT OUT (OCTOBER 2026)

Decision: Financial Modeling Lab is now its own separately planned project. The former October 24 EquityTrace roadmap sections for integrated three-statement forecasts, DCF, LBO, merger/M&A modeling, and their learning-reference register have been transferred into that new project's governing Google Doc; they are NO LONGER part of EquityTrace's future feature backlog.

Standalone project plan: https://docs.google.com/document/d/1oFTzUuo8GyxLPZgI5a0BOm11pC1KrhbDg46QxAyt4B0/edit?usp=drivesdk

### EquityTrace — frozen v1.0 product identity

EquityTrace remains a point-in-time historical stock research and backtesting engine. It ingests official SEC statements and market data, records provenance/availability, derives interpretable financial factors, ranks companies, evaluates reproducible strategy portfolios historically, and provides research reports/exports. It does not forecast financial statements, simulate acquisition deals, run LBOs, or perform DCF scenario valuation as core features.

Current delivery: finish the existing v1 research report/export and productization work; reconcile draft release-candidate and correctness PRs against live GitHub before merging or claiming release. Do not expand current release scope or restart work to support a separate modeling engine. Previously documented 2026-09 checkpoint commits are historical, not live status.

### Future data-export interface only — explicitly optional

If and when Financial Modeling Lab needs actual history, EquityTrace may offer a small versioned, read-only export of canonical annual historical statements with issuer/CIK, fiscal period, currency/units, SEC fact concept, accession, and public available-at timestamp. Flat-file CSV/JSON is enough. Financial Modeling Lab must run without EquityTrace first; no direct DuckDB schema coupling, shared internal modules, or automatic v1 work item.

The separate Quant Research & Trading Platform roadmap remains distinct for strategy simulation/execution research. Historical EquityTrace factors, hypothetical financial scenarios, and trade execution are different systems with explicit boundaries.

### Change control

This scope split supersedes EquityTrace's previously added “POST-v1 FINANCIAL MODELING EXPANSION” and “FINANCIAL MODELING LEARNING / REFERENCE REGISTER” sections. All detailed forecast/deal-model acceptance tests, references, and staged implementation guidance now live in Financial Modeling Lab's own plan. No code, GitHub PR, or feature is implemented by this documentation decision.
