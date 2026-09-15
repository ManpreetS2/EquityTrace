"""CLI commands for native research portfolio backtests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from equitytrace.config import ConfigurationError, Settings, clear_settings_cache, get_settings
from equitytrace.database import initialize_database
from equitytrace.factors.registry import UnknownFactorError
from equitytrace.market.models import PriceAdjustmentMode
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.portfolio.models import (
    BacktestRequest,
    BacktestResult,
    PortfolioBaseline,
    PortfolioRunStatus,
    PortfolioSchedule,
)

console = Console()
err_console = Console(stderr=True)

portfolio_app = typer.Typer(
    name="portfolio",
    help="Native research portfolio construction and weight-return backtests.",
    no_args_is_help=True,
)

_BASELINE_ALIASES = {
    "equal-weight": PortfolioBaseline.EQUAL_WEIGHT,
    "equal_weight": PortfolioBaseline.EQUAL_WEIGHT,
    "inverse-vol": PortfolioBaseline.INVERSE_VOL,
    "inverse_vol": PortfolioBaseline.INVERSE_VOL,
    "inverse-volatility": PortfolioBaseline.INVERSE_VOL,
}


def _user_error(message: str, code: int = 1) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code)


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {label} '{value}'. Use YYYY-MM-DD.") from exc


def _parse_dates(value: str | None) -> tuple[date, ...]:
    if not value:
        return ()
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return tuple(_parse_date(part, "explicit date") for part in parts)


def _parse_baseline(value: str) -> PortfolioBaseline:
    key = value.strip().lower()
    baseline = _BASELINE_ALIASES.get(key)
    if baseline is None:
        raise ValueError(f"Unknown baseline '{value}'. Use equal-weight or inverse-vol.")
    return baseline


def _parse_schedule(value: str) -> PortfolioSchedule:
    key = value.strip().lower()
    try:
        return PortfolioSchedule(key)
    except ValueError as exc:
        raise ValueError("schedule must be monthly, quarterly, or explicit.") from exc


@portfolio_app.command("backtest")
def backtest_cmd(
    tickers: Annotated[
        str,
        typer.Option("--tickers", help="Comma-separated research universe."),
    ],
    factor_name: Annotated[str, typer.Option("--factor", help="Registered factor name.")],
    start: Annotated[str, typer.Option("--start", help="Inclusive start date YYYY-MM-DD.")],
    end: Annotated[str, typer.Option("--end", help="Inclusive end date YYYY-MM-DD.")],
    top_n: Annotated[int, typer.Option("--top-n", help="Exact number of names to hold.")] = 1,
    baseline: Annotated[
        str,
        typer.Option("--baseline", help="equal-weight or inverse-vol."),
    ] = "equal-weight",
    schedule: Annotated[
        str,
        typer.Option("--schedule", help="monthly, quarterly, or explicit."),
    ] = "monthly",
    explicit_dates: Annotated[
        str | None,
        typer.Option(
            "--explicit-dates",
            help=(
                "Comma-separated YYYY-MM-DD decision sessions when schedule=explicit. "
                "Duplicates collapse first-seen; every remaining date must be an "
                "in-window stored calendar session with a later effective session "
                "on or before --end."
            ),
        ),
    ] = None,
    calendar_symbol: Annotated[
        str,
        typer.Option("--calendar-symbol", help="Reference calendar symbol."),
    ] = "SPY",
    benchmark: Annotated[
        str | None,
        typer.Option("--benchmark", help="Optional adjusted-close benchmark; empty to disable."),
    ] = "SPY",
    cost_bps: Annotated[
        float,
        typer.Option("--cost-bps", help="Symmetric cost in basis points."),
    ] = 10.0,
    initial_nav: Annotated[float, typer.Option("--initial-nav", help="Starting NAV.")] = 1.0,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Run a deterministic weight-return research backtest."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    ticker_list = tuple(part.strip() for part in tickers.split(",") if part.strip())
    if not ticker_list:
        _user_error("Provide at least one ticker via --tickers.")
    try:
        request = BacktestRequest(
            tickers=ticker_list,
            factor=factor_name,
            top_n=top_n,
            baseline=_parse_baseline(baseline),
            start_date=_parse_date(start, "start"),
            end_date=_parse_date(end, "end"),
            schedule=_parse_schedule(schedule),
            calendar_symbol=calendar_symbol,
            benchmark_symbol=None if benchmark is not None and not benchmark.strip() else benchmark,
            cost_bps=cost_bps,
            initial_nav=initial_nav,
            explicit_dates=_parse_dates(explicit_dates),
            adjustment_mode=PriceAdjustmentMode.ALL,
        )
    except (ValueError, UnknownFactorError) as exc:
        _user_error(str(exc))

    db = initialize_database(path)
    try:
        with db.session() as conn:
            result = BacktestEngine(conn).run(request)
    except ConfigurationError as exc:
        _user_error(str(exc))

    _print_result(result)
    if result.status is PortfolioRunStatus.SUCCESS:
        return
    if result.status is PortfolioRunStatus.UNAVAILABLE:
        raise typer.Exit(2)
    raise typer.Exit(1)


def _load_settings() -> Settings:
    clear_settings_cache()
    try:
        return get_settings()
    except ConfigurationError as exc:
        _user_error(str(exc))
        raise


def _print_result(result: BacktestResult) -> None:
    request = result.request
    metrics = result.metrics
    status = result.status.value
    reason = result.failure_reason or "—"
    if result.status is not PortfolioRunStatus.SUCCESS:
        err_console.print(
            Panel.fit(
                f"status={status}\nreason={reason}\nrun={result.run_id}",
                title="portfolio backtest",
            )
        )
        return
    assert metrics is not None
    console.print(
        Panel.fit(
            f"Run [bold]{result.run_id}[/bold] · {status}\n"
            f"{request.start_date.isoformat()} → {request.end_date.isoformat()}\n"
            f"universe={len(result.universe)} · factor={request.factor} · top_n={request.top_n}\n"
            f"baseline={request.baseline.value} · calendar={request.calendar_symbol}\n"
            f"benchmark={request.benchmark_symbol or '—'} · initial NAV={request.initial_nav:.6g}",
            title="portfolio backtest",
        )
    )
    table = Table()
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Final NAV", _fmt(result.final_nav))
    table.add_row("Total return", _fmt(metrics.total_return))
    table.add_row("Annualized return", _fmt(metrics.annualized_return))
    table.add_row("Volatility", _fmt(metrics.annualized_volatility))
    table.add_row("Sharpe (rf=0)", _fmt(metrics.sharpe_ratio))
    table.add_row("Max drawdown", _fmt(metrics.max_drawdown))
    table.add_row("Gross turnover", _fmt(metrics.gross_turnover))
    table.add_row("Transaction costs", _fmt(metrics.transaction_cost_total))
    table.add_row("Successful rebalances", str(metrics.successful_rebalance_count))
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow] " + "; ".join(result.warnings))


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.6g}"
