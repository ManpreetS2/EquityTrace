# EquityTrace — Project Charter and Planning Index

> **Canonical in-repository planning entry point.** Read this file and its linked plans before proposing milestones, UI work, integration decisions, or changes to scope. The original Google Docs have been mirrored in full below; they remain linked for provenance, not as an excuse to rely on older conversation summaries.

## Complete source plans

1. **[EquityTrace — Portfolio, Backtesting & Quant Expansion Master Plan](docs/plans/EQUITYTRACE_MASTER_PLAN.md)** — the complete EquityTrace product roadmap, portfolio/backtest guardrails, source/license evaluations, repository-map rules, dated milestone decisions, and the **October 2026 Financial Modeling Lab scope split**. [Original Google Doc](https://docs.google.com/document/d/1nh3J0-ujhv3kTDxw7GqlP7JLMk8NUC5Iv5xv6Hb05v4).
2. **[Quant Research & Trading Platform — Architecture & Open-Source Integration Plan](docs/plans/QUANT_PLATFORM_ARCHITECTURE_PLAN.md)** — the complete separate, future trading-platform architecture: EquityTrace integration boundary, skfolio, NautilusTrader, optional Kronos, Freqtrade/Hummingbot as references, licenses, phased development, and cross-project governance. [Original Google Doc](https://docs.google.com/document/d/1sL6AGEbC-cL8rOH0ZFD3fSjqYM_Ad0OF4RPpTVpS41o).

Both files are complete Markdown conversions of the **live Google Docs as read on 2026-10-26**, not abbreviated summaries. Original text paragraph order is retained, with heading and bullet markers converted to Markdown. Each file records the Google Docs revision and source modification date. No document tables were present.

## Planning precedence and source of truth

- **Implemented reality:** Live source code, migrations, tests, CI, open PRs, and releases are authoritative for what **actually exists**. A dated plan or old release note is not evidence that a feature is implemented, merged, or released.
- **EquityTrace product scope:** The [EquityTrace master plan](docs/plans/EQUITYTRACE_MASTER_PLAN.md) governs through v1.0. Within that plan, later explicit scope-change and supersession decisions take precedence over older historical execution entries.
- **Future execution/trading system:** The [Quant Research & Trading Platform plan](docs/plans/QUANT_PLATFORM_ARCHITECTURE_PLAN.md) governs *that separate project*. Do not import its optional models, connectors, broker functionality, or execution stack into EquityTrace merely because they are described there.
- **Repository navigation:** [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) describes code ownership, flows, boundaries, and tests; [docs/roadmap.md](docs/roadmap.md) retains the repository's release tracking. When a map differs from the code, correct the map. If a plan conflicts with current requirements, record an explicit decision instead of silently overwriting history.

## EquityTrace v1.0 boundary

EquityTrace is a Python-first, transparent, **point-in-time historical equity research** platform:

- SEC/XBRL ingestion, security/issuer identity, fact provenance, accepted/public availability timestamps, and canonical statements.
- Interpretable fundamental, valuation, momentum, and risk research; transparent and deterministic factor rankings.
- Reproducible portfolio/backtest research, explicit assumptions, and inspectable strategy inputs/outputs.
- A truthful, usable research workflow, including typed strategies and evidence-bearing reports and exports.

**Integrity is mandatory:** Never use future-unavailable evidence, silently fill missing financial facts, collapse ambiguous share classes, conceal restatements/corporate-action limitations, publish partially replaced SEC evidence as trustworthy, or claim crash safety without tests. Unavailable with a reason is better than an invented result.

**Current UI reality:** The repository contains a CLI, typed strategy JSON, and standalone HTML reports; these are not an interactive stock dashboard. The historic roadmap names a v1.0 "strategy builder and research interface," while the October 2026 frozen-scope decision prioritizes completing the existing v1 report/export and productization work. **Do not silently infer that a browser UI is already built or automatically approved as a new release requirement.** Decide and record the minimal interactive-UI scope explicitly before undertaking UI implementation or declaring that promise satisfied.

**Excluded from EquityTrace v1:** Price-prediction promises, LLM stock picking, personalized trading advice, live order execution, and a large new web framework without a separately approved bounded need. Financial Modeling Lab is now a **separate project**, not a post-v1 EquityTrace feature backlog; historical financial modeling and deal simulation live in its own plan, linked in the master document.

## Delivery and change control

1. Inspect live GitHub `main`, active and stacked PRs, exact SHAs, CI, [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md), and relevant source/tests **before** any implementation plan.
2. Choose **one primary core milestone at a time**. Keep changes bounded. Prefer fewer cohesive implementation prompts, explicit non-goals, and reviewable branch/PR work.
3. Require offline tests, Ruff, formatting, strict Mypy, package build/smoke, reproducibility and determinism, migration safety, provenance/PIT checks, and domain-specific failure/recovery gates.
4. Independently verify code, diff, precise head SHA, and CI; do not treat an agent's summary as proof. **Do not merge, tag, or release without explicit authorization.**
5. If architecture, module ownership, data flow, persistence, tests, or CLI entry points change, update [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) in the same PR where practical.
6. **Going forward, update these GitHub Markdown plans in a reviewable PR when project decisions change.** The original Google Docs are preserved as source references. If they are edited later, explicitly compare revisions and synchronize changes; never silently overwrite the repository's decisions.

## Source synchronization record

| Canonical Markdown file | Source | Source last modified (UTC) | Imported text paragraphs |
| --- | --- | --- | ---: |
| [EquityTrace master plan](docs/plans/EQUITYTRACE_MASTER_PLAN.md) | [Google Doc](https://docs.google.com/document/d/1nh3J0-ujhv3kTDxw7GqlP7JLMk8NUC5Iv5xv6Hb05v4) | 2026-10-25 06:37 | 185 |
| [Quant platform architecture plan](docs/plans/QUANT_PLATFORM_ARCHITECTURE_PLAN.md) | [Google Doc](https://docs.google.com/document/d/1sL6AGEbC-cL8rOH0ZFD3fSjqYM_Ad0OF4RPpTVpS41o) | 2026-09-15 18:19 | 176 |

The plans contain historical dated statements by design. Use live GitHub evidence for current release status.
