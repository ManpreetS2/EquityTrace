<!--
Canonical Markdown planning mirror for EquityTrace project governance.
Source: https://docs.google.com/document/d/1sL6AGEbC-cL8rOH0ZFD3fSjqYM_Ad0OF4RPpTVpS41o
Google Docs revision: AHj4eMTwPW9mfhF-HVXn95RFuH41ZP4bFp8MPveoqTA7kCncdlc_4z3t1GPL8Od6TugiXXV0A6TNjtUMHOawiUgYeydV_URSw_9Huas1DMI
Source modified (UTC): 2026-09-15T18:19:36.881Z
Original text paragraphs: 176
Body text preserved in source order. Heading/list prefixes use Markdown formatting.
Historical milestone/status notes are not current GitHub status claims.
-->
# Quant Research & Trading Platform — Architecture & Open-Source Integration Plan

*Concept architecture • September 2026*

## Purpose

Define a separate CS × Finance platform for market research, portfolio construction, strategy evaluation, paper trading, and eventually controlled live execution. This project should consume outputs from EquityTrace without bloating EquityTrace itself.

## TARGET IDENTITY

A modular quantitative research and execution platform where research signals, predictive models, portfolio construction, simulation, and execution remain distinct and testable.

## REFERENCE ARCHITECTURE

EquityTrace fundamentals / factors

        +

market data / technical features

        +

optional Kronos forecasts

        ↓

Signal normalization + provenance

        ↓

skfolio portfolio/risk layer

        ↓

strategy specification

        ↓

NautilusTrader simulation / paper / live execution

        ↓

monitoring, attribution, reconciliation, risk controls

Optional crypto-specific research/execution lanes may study Freqtrade or Hummingbot, but should not become dependencies unless the project deliberately targets crypto automation.

## CORE ENGINE CANDIDATE — NAUTILUSTRADER

GitHub: https://github.com/nautechsystems/nautilus_trader

Docs: https://nautilustrader.io/docs/

License: LGPL-3.0-only.

Role: event-driven simulation, paper/live execution, venue/data adapters, order lifecycle, market data, reconciliation, and multi-asset/multi-venue infrastructure.

Why it is the strongest foundation

- Rust-native production-grade engine with Python control layer.

- Same strategy/execution architecture across research simulation and live execution, reducing research-to-production divergence.

- Supports high-resolution event-driven backtesting, multiple venues/instruments/strategies, advanced order types, and modular adapters.

- Existing integrations span crypto plus traditional venues/providers such as Interactive Brokers and Databento.

Adoption rule

Treat NautilusTrader as the execution/simulation engine, not as the entire product. Build the user-facing research, strategy specification, experiment tracking, portfolio analysis, and explainability layer separately.

Do not modify upstream source unless necessary. Prefer adapters and extension points; review LGPL obligations before distributing modifications or tightly linked derivative code.

## PORTFOLIO / RISK LAYER — SKFOLIO

GitHub: https://github.com/skfolio/skfolio

Docs: https://skfolio.org/

License: BSD 3-Clause.

Role: portfolio optimization, risk measures, factor/prior estimators, constraints, cross-validation, model selection, and stress testing.

Recommended uses

- Equal-weight and inverse-volatility baselines.

- Mean-risk and maximum-ratio portfolios.

- Hierarchical risk parity / nested clusters.

- Risk budgeting and CVaR-oriented allocation.

- Black-Litterman and uncertainty-aware priors.

- Transaction costs, turnover constraints, tracking-error constraints, group/weight/cardinality limits.

- Walk-forward and purged validation.

Rule

A forecast or factor is not a portfolio. Every strategy should pass through explicit position sizing, risk, turnover, and constraints before simulated execution.

## PREDICTIVE SIGNAL RESEARCH — KRONOS

GitHub: https://github.com/shiyu-coder/Kronos

Model hub: https://huggingface.co/NeoQuasar

License: MIT repository code; verify model/tokenizer artifact licenses individually.

Role: optional probabilistic OHLCV/K-line forecasting research.

Use cases

- Compare forecast horizons across assets and regimes.

- Produce probabilistic market features rather than direct trade commands.

- Fine-tune only after establishing clean baseline evaluation.

- Batch inference across a controlled universe when GPU resources permit.

Hard rule

Kronos never sends orders directly. Its output becomes a versioned forecast feature that must be evaluated, normalized, risk-adjusted, and tested out of sample before it can influence a strategy.

Minimum evaluation

- walk-forward splits

- locked out-of-sample holdout

- naive persistence/random/simple momentum baselines

- transaction costs and turnover

- calibration / MAE / directional metrics appropriate to the experiment

- regime robustness

- no adjusted-data or universe leakage

- reproducible model/tokenizer/version/config metadata

## CRYPTO STRATEGY LAB REFERENCE — FREQTRADE

GitHub: https://github.com/freqtrade/freqtrade

Docs: https://www.freqtrade.io/

License: GPL-3.0.

Role: rapid Python crypto strategy experimentation and architecture/testing inspiration.

Useful ideas

- Dry-run before live trading.

- Backtesting and hyperparameter optimization.

- Strategy configuration and repeatable experiment workflows.

- Look-ahead analysis and recursive-analysis checks.

- Web/Telegram monitoring patterns.

- Explicit exchange compatibility and operational requirements.

When to actually use it

If the goal becomes “build and test a crypto directional strategy quickly,” run Freqtrade as its own tool/project rather than reimplementing its full stack.

License boundary

Do not copy GPL implementation into a differently licensed proprietary/MIT platform without deliberate legal/license review. Studying concepts and independently implementing similar testing safeguards is preferable.

## CRYPTO MARKET-MAKING / CONNECTOR REFERENCE — HUMMINGBOT

GitHub: https://github.com/hummingbot/hummingbot

Docs: https://hummingbot.org/

Condor AI harness: https://github.com/hummingbot/condor

License: Hummingbot is Apache 2.0; verify Condor separately.

Role: crypto exchange connectors, market making, arbitrage, grids, TWAP, liquidity provision, and deterministic execution patterns.

Useful architecture

LLM/research agent → proposed structured action → deterministic risk/validation layer → execution engine.

This is the pattern to study from Hummingbot/Condor. An AI system may generate hypotheses or propose actions, but credentials, risk limits, order validation, idempotency, and execution must remain deterministic.

When to use Hummingbot directly

Only if the project specifically targets crypto market making, cross-exchange arbitrage, DEX/CEX liquidity, or existing Hummingbot connector coverage. Do not use it merely because it has many exchanges.

## PROJECT PHASES

PHASE 0 — Research-only foundation

- Market-data normalization.

- Experiment registry and reproducible configs.

- Signal contract with provenance and timestamps.

- Baseline strategies and no live credentials.

- Import EquityTrace factor outputs through a stable interface rather than database coupling.

PHASE 1 — Portfolio/risk

- Add skfolio adapter.

- Implement constraints, risk budgets, turnover/cost model, and benchmark comparison.

- Add portfolio attribution and failure reporting.

PHASE 2 — Deterministic backtesting

- Choose/validate NautilusTrader as the primary event-driven engine.

- Model order fills, slippage, fees, latency assumptions, calendars, corporate actions, and capital constraints explicitly.

- Add leakage audit and reproducible seeded test scenarios.

PHASE 3 — Experimental forecasting

- Add Kronos behind an optional research adapter.

- Record every model/version/config/data-window combination.

- Require out-of-sample evidence before forecasts can enter a portfolio strategy.

PHASE 4 — Paper trading

- No real capital.

- Add provider/venue adapters, connection health, reconciliation, kill switch, max exposure, max order size, and daily loss limits.

- Compare simulated expectations vs paper-trading behavior.

PHASE 5 — Controlled live execution

Only after long paper-trading validation.

- Human opt-in.

- Explicit capital/risk limits.

- Secrets isolated from strategy/model code.

- Deterministic order validation.

- Idempotent order intents.

- Reconciliation against broker/exchange truth.

- Manual emergency stop.

- Full event/audit log.

## AI / AGENT BOUNDARY

Allowed

- research summaries

- experiment generation

- strategy hypothesis generation

- code assistance

- anomaly explanation

- post-trade attribution summaries

- proposed structured actions for human or deterministic-policy review

Not allowed by default

- unrestricted autonomous live orders

- AI-controlled credential management

- silent leverage/risk-limit changes

- retries of non-idempotent orders because an LLM timed out

- using model confidence as the sole risk control

## PROJECT SEPARATION

EquityTrace

Purpose: point-in-time explainable fundamental research and transparent factor/ranking system.

Quant Research & Trading Platform

Purpose: combine research signals, portfolio construction, simulation, risk, and controlled execution.

Jarvis

Purpose: optional human-facing research/monitoring interface. Jarvis may explain system state or request approved actions, but does not bypass the quant platform’s deterministic risk/execution layer.

## SOURCE / LICENSE REGISTER

- NautilusTrader — https://github.com/nautechsystems/nautilus_trader — LGPL-3.0-only.

- Hummingbot — https://github.com/hummingbot/hummingbot — Apache 2.0.

- Hummingbot Condor — https://github.com/hummingbot/condor — verify current repository license before reuse.

- Freqtrade — https://github.com/freqtrade/freqtrade — GPL-3.0.

- skfolio — https://github.com/skfolio/skfolio — BSD 3-Clause.

- Kronos — https://github.com/shiyu-coder/Kronos — MIT repository code; verify model licenses.

## FIRST PROTOTYPE RECOMMENDATION

Do not start with live trading. Build one reproducible research pipeline:

EquityTrace factor snapshot + market data → simple signal → skfolio portfolio → NautilusTrader backtest → attribution report.

Then add Kronos as one optional competing signal and compare it against simple baselines. This proves the architecture without putting capital at risk.

## BOTTOM LINE

The strongest stack is not “five trading bots combined.” It is a layered system: EquityTrace for explainable fundamentals, Kronos only for experimental forecasts, skfolio for portfolio/risk, NautilusTrader for event-driven simulation/execution, with Freqtrade and Hummingbot serving as specialized reference/tools for crypto strategy and execution patterns.

## CROSS-PROJECT ROADMAP GOVERNANCE

The EquityTrace master plan and the Quant Research & Trading Platform plan are maintained as separate but coordinated roadmaps. New architectural decisions should be recorded in the governing plan before implementation begins.

### Repository-map rule

Every active repository in this project family should maintain a concise in-repo project map describing entry points, module responsibilities, data flows, persistence, tests, invariants, and integration boundaries. EquityTrace's canonical map is `docs/PROJECT_MAP.md`; the separate quant-platform repository should adopt its own equivalent when created. Maps should be updated in the same PR as architecture-affecting code when practical and are used to bound Cursor context, not to replace live repository verification. Cross-project integration must occur through explicit versioned contracts or adapters; do not couple the future quant platform directly to EquityTrace's DuckDB schema.

### Planning precedence

For EquityTrace scope through v1.0, the EquityTrace master plan governs. For post-v1 signal aggregation, portfolio/risk orchestration, simulation, paper trading, and controlled execution, this document governs. If a future plan changes that boundary, update both documents before implementation

## CURRENT DELIVERY SEQUENCING

As of 2026-09-15, EquityTrace v0.3.1 is released and its CI/package hardening is merged. Active implementation remains inside EquityTrace on v0.3c: first the native deterministic portfolio/backtest foundation, then the narrow skfolio adapter, followed by v1.0 productization. Do not begin the separate execution-platform repository or NautilusTrader/Kronos implementation as part of this delivery sequence. The future quant platform should consume stable, explicit EquityTrace research contracts only after the relevant v1.0 boundaries are intentionally defined.

.
