"""CLI commands for reproducible research strategies (v1.0 Milestone 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from equitytrace.config import ConfigurationError, Settings, clear_settings_cache, get_settings
from equitytrace.database import Database, initialize_database
from equitytrace.factors.registry import get_factor
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.portfolio.models import PortfolioRunStatus
from equitytrace.repositories.portfolio import PortfolioRepository
from equitytrace.strategy.io import StrategyIOError, load_strategy
from equitytrace.strategy.models import fingerprint_request, fingerprint_strategy

console = Console()
err_console = Console(stderr=True)

research_app = typer.Typer(
    name="research",
    help="Typed research strategies: validate, run, and show persisted results.",
    no_args_is_help=True,
)


def _user_error(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code)


def _load_settings() -> Settings:
    clear_settings_cache()
    try:
        return get_settings()
    except ConfigurationError as exc:
        _user_error(str(exc))
        raise


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.6g}"


@research_app.command("validate")
def validate_cmd(
    strategy_path: Annotated[
        Path,
        typer.Option("--strategy", help="Path to a ResearchStrategy JSON file."),
    ],
) -> None:
    """Validate strategy JSON and print the economic fingerprint (no database)."""
    try:
        strategy = load_strategy(strategy_path)
    except StrategyIOError as exc:
        _user_error(str(exc))
    request = strategy.to_backtest_request()
    digest = fingerprint_strategy(strategy)
    direction = get_factor(strategy.factor).ranking_direction
    console.print(
        Panel.fit(
            f"name=[bold]{strategy.name}[/bold]\nfingerprint={digest}",
            title="research validate",
        )
    )
    table = Table()
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("universe", ", ".join(strategy.tickers))
    table.add_row("factor", strategy.factor)
    table.add_row("ranking_direction", direction)
    table.add_row("top_n", str(strategy.top_n))
    table.add_row("baseline", strategy.baseline.value)
    table.add_row("schedule", strategy.schedule.value)
    table.add_row(
        "date_range",
        f"{strategy.start_date.isoformat()} → {strategy.end_date.isoformat()}",
    )
    if strategy.explicit_dates:
        table.add_row(
            "explicit_dates",
            ", ".join(day.isoformat() for day in strategy.explicit_dates),
        )
    table.add_row("calendar", strategy.calendar_symbol)
    table.add_row("benchmark", strategy.benchmark_symbol or "—")
    table.add_row("cost_bps", _fmt(strategy.cost_bps))
    table.add_row("initial_nav", _fmt(strategy.initial_nav))
    table.add_row("provider", strategy.provider.value)
    table.add_row("adjustment_mode", request.adjustment_mode.value)
    console.print(table)


@research_app.command("run")
def run_cmd(
    strategy_path: Annotated[
        Path,
        typer.Option("--strategy", help="Path to a ResearchStrategy JSON file."),
    ],
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Run a strategy through the existing BacktestEngine and persist the result."""
    try:
        strategy = load_strategy(strategy_path)
    except StrategyIOError as exc:
        _user_error(str(exc))
    request = strategy.to_backtest_request()
    digest = fingerprint_strategy(strategy)
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    db = initialize_database(path)
    try:
        with db.session() as conn:
            result = BacktestEngine(conn).run(request)
    except ConfigurationError as exc:
        _user_error(str(exc))

    console.print(
        Panel.fit(
            f"strategy=[bold]{strategy.name}[/bold]\n"
            f"fingerprint={digest}\n"
            f"run_id={result.run_id}\n"
            f"status={result.status.value}\n"
            f"package_version={result.package_version}\n"
            f"factor={request.factor} · baseline={request.baseline.value}\n"
            f"{request.start_date.isoformat()} → {request.end_date.isoformat()}"
            + (f"\nfailure_reason={result.failure_reason}" if result.failure_reason else ""),
            title="research run",
        )
    )
    if result.status is PortfolioRunStatus.SUCCESS and result.metrics is not None:
        metrics = result.metrics
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
    if result.status is PortfolioRunStatus.SUCCESS:
        return
    if result.status is PortfolioRunStatus.UNAVAILABLE:
        raise typer.Exit(2)
    raise typer.Exit(1)


@research_app.command("show")
def show_cmd(
    run_id: Annotated[str, typer.Argument(help="Persisted portfolio run_id.")],
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Show a persisted research run and recompute its economic fingerprint."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    db = Database(path)
    with db.session(read_only=True) as conn:
        loaded = PortfolioRepository(conn).get_run(run_id.strip())
    if loaded is None:
        _user_error(f"No portfolio run found for run_id={run_id!r}.")
    result = loaded

    digest = fingerprint_request(result.request)
    request = result.request
    metrics = result.metrics
    audit = result.audit
    console.print(
        Panel.fit(
            f"run_id={result.run_id}\n"
            f"fingerprint={digest}\n"
            f"package_version={result.package_version}\n"
            f"status={result.status.value}",
            title="research show",
        )
    )
    table = Table()
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("failure_reason", result.failure_reason or "—")
    table.add_row("universe", ", ".join(request.tickers))
    table.add_row("factor", request.factor)
    table.add_row("top_n", str(request.top_n))
    table.add_row("baseline", request.baseline.value)
    table.add_row("schedule", request.schedule.value)
    table.add_row(
        "date_range",
        f"{request.start_date.isoformat()} → {request.end_date.isoformat()}",
    )
    if request.explicit_dates:
        table.add_row(
            "explicit_dates",
            ", ".join(day.isoformat() for day in request.explicit_dates),
        )
    table.add_row("calendar", request.calendar_symbol)
    table.add_row("benchmark", request.benchmark_symbol or "—")
    table.add_row("cost_bps", _fmt(request.cost_bps))
    table.add_row("initial_nav", _fmt(request.initial_nav))
    table.add_row("provider", request.provider.value)
    table.add_row("adjustment_mode", request.adjustment_mode.value)
    table.add_row("final_nav", _fmt(result.final_nav))
    if metrics is not None:
        table.add_row("total_return", _fmt(metrics.total_return))
        table.add_row("annualized_return", _fmt(metrics.annualized_return))
        table.add_row("annualized_volatility", _fmt(metrics.annualized_volatility))
        table.add_row("sharpe_ratio", _fmt(metrics.sharpe_ratio))
        table.add_row("max_drawdown", _fmt(metrics.max_drawdown))
        table.add_row("gross_turnover", _fmt(metrics.gross_turnover))
        table.add_row("transaction_cost_total", _fmt(metrics.transaction_cost_total))
        table.add_row("successful_rebalances", str(metrics.successful_rebalance_count))
    table.add_row("warnings", "; ".join(result.warnings) if result.warnings else "—")
    if audit is None:
        table.add_row("audit", "—")
    else:
        table.add_row("audit_passed", str(audit.passed))
        table.add_row(
            "audit_failures",
            "; ".join(audit.failures) if audit.failures else "—",
        )
    table.add_row("rebalance_count", str(len(result.rebalances)))
    table.add_row("equity_point_count", str(len(result.equity)))
    console.print(table)
    # Retrieval succeeded regardless of historical research status.
