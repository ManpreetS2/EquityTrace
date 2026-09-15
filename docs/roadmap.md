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
v0.3.1). This slice is the foundation only; v0.3c is not complete until the
skfolio adapter lands.

* Latest-FY-per-issuer factor selection and exact top-N
* Equal weight and inverse-volatility baselines
* Reference-calendar decision vs target-effective timing
* Weight drift, gross turnover, symmetric bps costs
* Equity curve, compact metrics, leakage audit, DuckDB persistence
* CLI: `portfolio backtest`

Not in this slice: skfolio / minimum-variance optimizer.

### v1.0

* Strategy builder
* Research interface
* Equity curves
* Drawdown charts
* Filing evidence
* Exportable reports
