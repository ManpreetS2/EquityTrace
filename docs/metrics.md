# Research metrics

Formulas and point-in-time rules for EquityTrace v0.3.2.dev0 valuation factors,
market-window analytics, and native research backtests. Ambiguous inputs return `UNAVAILABLE` rather than a
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

* Formula: `FCF / PIT market cap`
* Market cap: `raw close × PIT shares`
* Inputs: snapshot `free_cash_flow`; PIT market cap
* Price mode: raw
* Ranking: higher is better
* PIT: filings and market cap both filtered by `as_of`
* Unavailable: missing FCF; missing/stale/multi-class/non-positive market cap;
  currency mismatch
* Limitation: FCF is the selected fiscal period, not TTM

### Price to earnings (`price_to_earnings`)

* Formula: `PIT market cap / annual net income`
* Inputs: `net_income`; PIT market cap
* Price mode: raw
* Lookback: the requested FY period only (no annualization)
* Ranking: lower is better
* PIT: as above
* Unavailable: quarterly period (`annual_period_required`); missing/non-positive
  net income; market cap unavailable; currency mismatch
* Limitation: FY only; no TTM quarter assembly; negative P/E is not returned

### Price to sales (`price_to_sales`)

* Formula: `PIT market cap / annual revenue`
* Inputs: `revenue`; PIT market cap
* Price mode: raw
* Ranking: lower is better
* Unavailable: quarterly period (`annual_period_required`); missing/non-positive
  revenue; market cap unavailable; currency mismatch
* Limitation: FY only; no TTM

### Price to book (`price_to_book`)

* Formula: `PIT market cap / stockholders_equity`
* Inputs: `stockholders_equity` (instant balance-sheet value); PIT market cap
* Price mode: raw
* Ranking: lower is better
* PIT: as above
* Unavailable: missing/non-positive equity; market cap unavailable; currency
  mismatch
* Limitation: annual or selected quarterly balance-sheet snapshot; still not a
  tangible-book variant

## Market metrics (stored daily bars)

These live under `src/equitytrace/market/analytics.py`. No provider HTTP calls.
Queries use a bounded ~550 calendar-day window and `PriceAdjustmentMode.ALL`.
Bars with `available_at > as_of` or `trading_date > as_of.date()` are excluded.
Default provider: `twelve_data`.

Required history is the full 253 adjusted closes (252 returns) unless noted.
Shorter samples are unavailable rather than relabeled as one-year metrics.

### 12-1 momentum (`momentum_12_1`)

* Formula: `bars[-22] / bars[-253] - 1`
* Observations: latest 253 adjusted closes
* End: `bars[-22]` (skips the latest 21 sessions)
* Ranking: higher is better
* Unavailable: fewer than 253 closes; start or end close `<= 0`; ambiguous duplicate
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

## Portfolio backtest (v0.3c)

Research weight-return model. Not an execution simulator. No fill prices or
share quantities.

Standing warnings: `provider_adjusted_history_not_vintage_pit`,
`survivorship_bias_possible`. Mixed issuer fiscal years add
`mixed_fiscal_periods`.

### Timing

* Decision session T0 uses the stored reference-calendar bar `available_at`.
* Target becomes effective at the next reference-calendar session T1.
* T0→T1 the old weights earn the interval return; T1 is marked, then the
  transition is applied.

### Latest FY signal

Each name uses only its latest annual period known at `decision_at`. If that
factor is unavailable, the name is excluded. Older fiscal years are not
substituted.

### Turnover

`gross_turnover = Σ_assets |target − current_drifted_weight|`

Cash is not a turnover leg. Missing names count as zero weight.

### Cost

`cost = pre_cost_nav × gross_turnover × (cost_bps / 10_000)`

`post_cost_nav = pre_cost_nav − cost`

Later unavailable rebalances: turnover 0, cost 0, drifted state continues.

### Inverse volatility

Intersect common **price dates** first, then 252 close-to-close returns on those
dates. Sample standard deviation. No forward fill.

### Performance

* total return = `final_nav / initial_nav − 1`
* annualized return = `(final_nav / initial_nav) ** (252 / N) − 1`
* volatility = sample stdev of session returns × `sqrt(252)`
* Sharpe (rf=0) = mean(session returns) / sample stdev × `sqrt(252)`
* max drawdown = `min(nav_event / running_peak − 1)` including pre-transition marks
