# Roadmap

### v0.1

* SEC ingestion
* Point-in-time storage
* CLI
* DuckDB

### v0.2

* Canonical financial statements
* XBRL concept mapping registry
* Annual / quarterly normalization + YTD derivation
* Restatement / amendment-aware snapshots
* Revenue growth
* Operating-margin change
* Free cash flow (+ FCF yield when market cap supplied)
* Debt change
* ROA
* Accrual quality
* Basic quality score
* Cross-sectional ranking
* CLI: `statements`, `factor`, `factors`, `rank`

### v0.3a / v0.3.0 — Market data foundation (completed)

* Provider-neutral daily OHLCV ingestion
* Twelve Data `/time_series` implementation
* Raw (`adjust=none`) and provider-adjusted (`adjust=all`) storage
* Market instruments + provider symbol mappings
* Point-in-time shares outstanding from SEC facts
* Historically safe market-cap calculations
* SPY / ETF benchmark price support
* CLI: `market ingest`, `market prices`, `market cap`, `market cap-series`

### v0.3b / v0.3.1 — Valuation, momentum, and risk (completed)

* Native FCF yield using stored PIT market cap
* P/E, price-to-sales, price-to-book
* 12-1 momentum; 1y volatility, beta, and max drawdown
* PIT-safe market metric ranking (beta has no default direction)
* Adversarial correctness hardening for valuation and market-window metrics
* CLI: `market analytics`, `market rank-metric`
* Formulas: `docs/metrics.md`

### v0.3c — Portfolio construction and backtesting (in review)

Native weight-return research backtester (`0.3.2.dev0`, latest release remains
v0.3.1). Native foundation = implemented; minimum-variance adapter = in review.
v0.3c is not complete until that adapter is independently reviewed and merged.

* Latest-FY factor selection per security/ticker and exact top-N
* Equal weight, inverse-volatility, and skfolio-backed minimum-variance baselines
* Reference-calendar decision vs target-effective timing
* Explicit schedules fail closed; monthly/quarterly use last in-window sessions
* Same-issuer multi-security universes are unavailable (no share-class guess)
* Weight drift, gross turnover, symmetric bps costs
* Equity curve, compact metrics, leakage audit, DuckDB persistence
* Native backtests currently require `adjustment_mode=all`
* CLI: `portfolio backtest`

Not in v0.3c: HRP, Black-Litterman, CVaR, risk budgeting, max-turnover
optimization, or skfolio transaction-cost modeling (EquityTrace costs stay native).

### v1.0

* Strategy builder
* Research interface
* Equity curves
* Drawdown charts
* Filing evidence
* Exportable reports
