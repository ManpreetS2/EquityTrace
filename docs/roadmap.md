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

### v0.3b — Valuation, momentum, and risk (this branch)

* Native FCF yield using stored PIT market cap
* P/E, price-to-sales, price-to-book
* 12-1 momentum; 1y volatility, beta, and max drawdown
* PIT-safe market metric ranking (beta has no default direction)
* CLI: `market analytics`, `market rank-metric`
* Formulas: `docs/metrics.md`

Not released. Package version is `0.3.1.dev0`.

### v0.3c — Portfolio construction and backtesting

* Portfolio construction
* Rebalancing
* Transaction costs
* Backtesting metrics

### v1.0

* Strategy builder
* Research interface
* Equity curves
* Drawdown charts
* Filing evidence
* Exportable reports
