# Research metrics

Formulas and point-in-time rules for EquityTrace v0.3b valuation factors and
market-window analytics. Ambiguous inputs return `UNAVAILABLE` rather than a
guess.

Provider-adjusted history is not a vendor-vintage PIT archive. Corporate-action
adjustments can be revised later. Valuation uses **raw** closes; momentum,
volatility, beta, and drawdown use **provider-adjusted** closes and always
carry warning `provider_adjusted_history_not_vintage_pit`.

## Valuation (fiscal-period factors)

These live under `src/equitytrace/factors/` and use `FactorEngine` /
`FinancialPeriod`. Market cap is resolved by `MarketCapService`:

* `market_date = as_of.date()`
* knowledge time = `as_of`
* raw close only
* PIT SEC shares, staleness, multi-class refusal

`--market-cap` remains a manual override and is labeled
`manual_market_cap_override` (not stored market-data provenance).

Canonical financials are USD. A non-USD market-cap currency is unavailable.

### FCF yield (`fcf_yield`)

* Formula: `period free cash flow / market capitalization`
* Inputs: snapshot `free_cash_flow`; PIT market cap
* Price mode: raw
* Ranking: higher is better
* PIT: filings and market cap both filtered by `as_of`
* Unavailable: missing FCF; missing/stale/multi-class/non-positive market cap;
  currency mismatch
* Limitation: FCF is the selected fiscal period, not TTM

### Price to earnings (`price_to_earnings`)

* Formula: `PIT market cap / selected-period net income`
* Inputs: `net_income`; PIT market cap
* Price mode: raw
* Lookback: the requested FY period only (no annualization)
* Ranking: lower is better
* PIT: as above
* Unavailable: quarterly period (`annual_period_required`); missing/non-positive
  net income; market cap unavailable; currency mismatch
* Limitation: no TTM quarter assembly; negative P/E is not returned

### Price to sales (`price_to_sales`)

* Formula: `PIT market cap / selected-period revenue`
* Inputs: `revenue`; PIT market cap
* Price mode: raw
* Ranking: lower is better
* Unavailable: quarterly period; missing/non-positive revenue; market cap
  unavailable; currency mismatch
* Limitation: FY only; no TTM

### Price to book (`price_to_book`)

* Formula: `PIT market cap / stockholders_equity`
* Inputs: `stockholders_equity` (instant balance-sheet value); PIT market cap
* Price mode: raw
* Ranking: lower is better
* PIT: as above
* Unavailable: missing/non-positive equity; market cap unavailable; currency
  mismatch
* Limitation: book value may be annual or quarterly; still not a tangible-book
  variant

## Market metrics (stored daily bars)

These live under `src/equitytrace/market/analytics.py`. No provider HTTP calls.
Queries use a bounded ~550 calendar-day window and `PriceAdjustmentMode.ALL`.
Bars with `available_at > as_of` or `trading_date > as_of.date()` are excluded.
Default provider: `twelve_data`.

Required history is the full 253 adjusted closes (252 returns) unless noted.
Shorter samples are unavailable rather than relabeled as one-year metrics.

### 12-1 momentum (`momentum_12_1`)

* Formula: `end_adjusted_close / start_adjusted_close - 1`
* Start: most recent 253 closes, `bars[-253]`
* End: `bars[-22]` (skips the latest 21 observations)
* Ranking: higher is better
* Unavailable: fewer than 253 closes; start close `<= 0`; ambiguous duplicate
  history

### 1y volatility (`volatility_1y`)

* Formula: sample stdev of 252 simple daily returns × `sqrt(252)`
* Return: `close_t / close_(t-1) - 1`
* Ranking: lower is better
* Unavailable: fewer than 253 closes / 252 returns; non-positive close

### 1y beta (`beta_1y`)

* Formula: `cov(asset_returns, benchmark_returns) / var(benchmark_returns)`
* Default benchmark: `SPY` (must already be stored; not downloaded)
* Process: intersect common price dates first, then simple returns on those
  consecutive shared intervals; no forward-fill; latest 252 matched returns.
  Beta aligns common price dates first so asset and benchmark returns cover
  identical intervals.
* Ranking: none by default (low/high beta is not universally better)
* Unavailable: missing benchmark; fewer than 252 aligned pairs; zero benchmark
  variance; non-positive close

### 1y max drawdown (`max_drawdown_1y`)

* Formula: `min(close_t / running_peak_t - 1)` over 253 closes
* Result is `<= 0` (`-0.10` is a 10% drawdown)
* Ranking: higher is better (`-0.10` beats `-0.50`)
* Unavailable: fewer than 253 closes; non-positive close
