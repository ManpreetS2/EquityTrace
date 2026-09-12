"""EquityTrace command-line interface."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from equitytrace import __version__
from equitytrace.cli_market import market_app
from equitytrace.config import ConfigurationError, Settings, clear_settings_cache, get_settings
from equitytrace.database import Database, initialize_database
from equitytrace.factors.engine import FactorEngine
from equitytrace.factors.registry import UnknownFactorError, list_factors
from equitytrace.financials.models import CanonicalValue, FinancialSnapshot
from equitytrace.financials.periods import parse_period
from equitytrace.financials.service import FinancialsError, FinancialsService
from equitytrace.repositories.facts import FactsRepository
from equitytrace.repositories.filings import FilingsRepository
from equitytrace.repositories.ingestion import IngestionError, IngestionRepository
from equitytrace.repositories.securities import SecuritiesRepository
from equitytrace.sec.client import SecClient, SecClientError, SecHttpError
from equitytrace.sec.tickers import resolve_ticker

app = typer.Typer(
    name="equitytrace",
    help=(
        "EquityTrace: transparent stock research backed by filings, factors, "
        "and historical evidence."
    ),
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(market_app, name="market")
console = Console()
err_console = Console(stderr=True)

_LEGACY_CLI_WARNING = (
    "DeprecationWarning: the 'filingedge' command is renamed to 'equitytrace' "
    "and will be removed in a future release. Use 'equitytrace' instead."
)


def deprecated_filingedge_cli() -> None:
    """Deprecated v0.1 CLI entry point; delegates to the EquityTrace CLI."""
    err_console.print(f"[yellow]{_LEGACY_CLI_WARNING}[/yellow]")
    app()


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_settings() -> Settings:
    clear_settings_cache()
    return get_settings()


def _user_error(message: str, code: int = 1) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code)


def _parse_as_of(value: str) -> datetime:
    text = value.strip()
    try:
        if "T" in text or " " in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        day = date.fromisoformat(text)
        return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD or an ISO datetime.") from exc


@app.callback()
def main(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable debug logging."),
    ] = False,
) -> None:
    """EquityTrace CLI."""
    _configure_logging(verbose)


@app.command("init-db")
def init_db(
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Initialize the DuckDB schema."""
    settings = _load_settings()
    path = database or settings.database_path
    initialize_database(path)
    console.print(f"[green]Initialized database[/green] at [cyan]{path}[/cyan]")


@app.command("resolve")
def resolve_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol, e.g. AAPL")],
) -> None:
    """Resolve a ticker to an SEC CIK and company name."""
    settings = _load_settings()
    try:
        with SecClient(settings) as client:
            resolved = resolve_ticker(client, ticker)
    except ConfigurationError as exc:
        _user_error(str(exc))
    except (SecClientError, SecHttpError) as exc:
        _user_error(str(exc))

    table = Table(title="Ticker Resolution", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Ticker", resolved.ticker)
    table.add_row("CIK", resolved.cik)
    table.add_row("Company", resolved.company_name)
    table.add_row("Exchange", resolved.exchange or "—")
    console.print(table)


@app.command("ingest")
def ingest_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol to ingest")],
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Ingest SEC submissions and Company Facts for a ticker."""
    settings = _load_settings()
    path = database or settings.database_path
    db = initialize_database(path)
    repo = IngestionRepository(db)

    try:
        with (
            SecClient(settings) as client,
            console.status(f"[bold green]Ingesting {ticker.upper()}..."),
        ):
            result = repo.ingest_ticker(client, ticker)
    except ConfigurationError as exc:
        _user_error(str(exc))
    except (SecClientError, SecHttpError, IngestionError) as exc:
        _user_error(str(exc))

    table = Table(title=f"Ingestion complete: {result.ticker}", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    table.add_row("CIK", result.cik)
    table.add_row("Legal name", result.legal_name)
    table.add_row("Issuers", str(result.issuer_count))
    table.add_row("Securities", str(result.security_count))
    table.add_row("Filings", str(result.filing_count))
    table.add_row("Facts", str(result.fact_count))
    table.add_row("Elapsed", f"{result.elapsed_seconds:.2f}s")
    table.add_row("Database", str(path))
    console.print(table)


@app.command("filings")
def filings_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol")],
    form: Annotated[
        str | None,
        typer.Option("--form", help="Filter by form type, e.g. 10-K"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max rows")] = 20,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """List stored filings for a ticker."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` and ingest first.")

    db = Database(path)
    with db.session(read_only=True) as conn:
        if SecuritiesRepository(conn).resolve_cik(ticker) is None:
            _user_error(
                f"No stored data for ticker {ticker.upper()}. Run `equitytrace ingest` first."
            )
        rows = FilingsRepository(conn).list_for_ticker(ticker, form=form, limit=limit)

    if not rows:
        console.print(f"No filings found for {ticker.upper()}.")
        raise typer.Exit(0)

    table = Table(title=f"Filings for {ticker.upper()}")
    table.add_column("Form")
    table.add_column("Filing date")
    table.add_column("Available at (UTC)")
    table.add_column("Accession")
    table.add_column("Report date")
    for filing in rows:
        table.add_row(
            filing.form,
            filing.filing_date.isoformat(),
            filing.available_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            filing.accession_number,
            filing.report_date.isoformat() if filing.report_date else "—",
        )
    console.print(table)


@app.command("facts")
def facts_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol")],
    concept: Annotated[
        str | None,
        typer.Option("--concept", help="Partial, case-insensitive concept match"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max rows")] = 20,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Search stored financial facts for a ticker."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` and ingest first.")

    db = Database(path)
    with db.session(read_only=True) as conn:
        if SecuritiesRepository(conn).resolve_cik(ticker) is None:
            _user_error(
                f"No stored data for ticker {ticker.upper()}. Run `equitytrace ingest` first."
            )
        rows = FactsRepository(conn).search(ticker, concept=concept, limit=limit)

    if not rows:
        console.print(f"No facts found for {ticker.upper()}.")
        raise typer.Exit(0)

    table = Table(title=f"Facts for {ticker.upper()}")
    table.add_column("Concept")
    table.add_column("Unit")
    table.add_column("Value", justify="right")
    table.add_column("End")
    table.add_column("Available at (UTC)")
    table.add_column("Form")
    for fact in rows:
        table.add_row(
            fact.concept,
            fact.unit,
            f"{fact.value:,.4g}",
            fact.end_date.isoformat() if fact.end_date else "—",
            fact.available_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            fact.form or "—",
        )
    console.print(table)


@app.command("facts-as-of")
def facts_as_of_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol")],
    as_of_date: Annotated[
        str,
        typer.Option("--date", help="Point-in-time date (YYYY-MM-DD or ISO datetime)"),
    ],
    concept: Annotated[
        str | None,
        typer.Option("--concept", help="Partial, case-insensitive concept match"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max rows")] = 50,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Query financial facts as they were known on a historical date."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` and ingest first.")

    try:
        as_of = _parse_as_of(as_of_date)
    except ValueError as exc:
        _user_error(str(exc))

    db = Database(path)
    with db.session(read_only=True) as conn:
        if SecuritiesRepository(conn).resolve_cik(ticker) is None:
            _user_error(
                f"No stored data for ticker {ticker.upper()}. Run `equitytrace ingest` first."
            )
        rows = FactsRepository(conn).get_facts_as_of(
            ticker,
            as_of,
            concept=concept,
            limit=limit,
        )

    console.print(
        Panel.fit(
            f"Point-in-time facts for [bold]{ticker.upper()}[/bold] as of "
            f"[cyan]{as_of.isoformat()}[/cyan]\n"
            f"Filter: available_at <= as_of ({len(rows)} rows)",
            title="facts-as-of",
        )
    )
    if not rows:
        console.print("No facts available at that point in time.")
        raise typer.Exit(0)

    table = Table()
    table.add_column("Concept")
    table.add_column("Unit")
    table.add_column("Value", justify="right")
    table.add_column("End")
    table.add_column("Available at (UTC)")
    table.add_column("Form")
    for fact in rows:
        table.add_row(
            fact.concept,
            fact.unit,
            f"{fact.value:,.4g}",
            fact.end_date.isoformat() if fact.end_date else "—",
            fact.available_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            fact.form or "—",
        )
    console.print(table)


@app.command("db-info")
def db_info_cmd(
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Display database path, counts, and latest successful ingestion."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")

    db = Database(path)
    stats = IngestionRepository(db).database_stats()
    latest = stats["latest_successful_ingestion"]

    table = Table(title="EquityTrace Database", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Database path", str(stats["database_path"]))
    table.add_row("Issuers", str(stats["issuer_count"]))
    table.add_row("Securities", str(stats["security_count"]))
    table.add_row("Filings", str(stats["filing_count"]))
    table.add_row("Facts", str(stats["fact_count"]))
    if latest is None:
        table.add_row("Latest successful ingestion", "—")
    else:
        finished = latest.finished_at.astimezone(UTC).isoformat() if latest.finished_at else "—"
        table.add_row(
            "Latest successful ingestion",
            f"{latest.ticker} ({latest.cik}) at {finished}",
        )
    console.print(table)


@app.command("version")
def version_cmd() -> None:
    """Print the EquityTrace version."""
    console.print(f"EquityTrace {__version__}")


@app.command("statements")
def statements_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol")],
    period: Annotated[
        str,
        typer.Option("--period", help="Fiscal period, e.g. FY2023 or Q3-2023"),
    ],
    as_of_date: Annotated[
        str | None,
        typer.Option("--as-of", help="Point-in-time date (YYYY-MM-DD or ISO datetime)"),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Show canonical financial statements for a ticker and period."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` and ingest first.")

    try:
        parsed_period = parse_period(period)
        as_of = _parse_as_of(as_of_date) if as_of_date else datetime.now(UTC)
    except ValueError as exc:
        _user_error(str(exc))

    # Ensure additive v0.2 tables exist for older local databases.
    db = initialize_database(path)
    try:
        with db.session() as conn:
            snapshot = FinancialsService(conn).get_snapshot(ticker, parsed_period, as_of=as_of)
    except FinancialsError as exc:
        _user_error(str(exc))

    _print_snapshot(snapshot)


@app.command("factor")
def factor_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol")],
    factor_name: Annotated[str, typer.Argument(help="Factor name, e.g. revenue-growth")],
    period: Annotated[
        str,
        typer.Option("--period", help="Fiscal period, e.g. FY2023 or Q3-2023"),
    ],
    as_of_date: Annotated[
        str | None,
        typer.Option("--as-of", help="Point-in-time date (YYYY-MM-DD or ISO datetime)"),
    ] = None,
    market_cap: Annotated[
        float | None,
        typer.Option(
            "--market-cap",
            help=(
                "Optional market-cap override for valuation factors. "
                "Stored PIT market cap is used when omitted."
            ),
        ),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Calculate one fundamental factor for a ticker."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` and ingest first.")

    try:
        parsed_period = parse_period(period)
        as_of = _parse_as_of(as_of_date) if as_of_date else datetime.now(UTC)
    except ValueError as exc:
        _user_error(str(exc))

    db = initialize_database(path)
    try:
        with db.session() as conn:
            result = FactorEngine(conn).calculate(
                ticker,
                factor_name,
                parsed_period,
                as_of=as_of,
                market_cap=market_cap,
            )
    except UnknownFactorError as exc:
        _user_error(str(exc))
    except FinancialsError as exc:
        _user_error(str(exc))

    table = Table(title=f"{result.factor} · {result.ticker}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("CIK", result.cik)
    table.add_row("Period", result.period.label())
    table.add_row("As of", result.as_of.astimezone(UTC).isoformat())
    table.add_row("Valid", "yes" if result.valid else "no")
    table.add_row(
        "Value",
        f"{result.value:.6g}" if result.value is not None else "—",
    )
    if result.unavailable_reason:
        table.add_row("Unavailable", result.unavailable_reason)
    table.add_row("Direction", result.ranking_direction)
    if result.source_filings:
        table.add_row("Source filings", ", ".join(result.source_filings))
    if result.warnings:
        table.add_row("Warnings", "; ".join(result.warnings))
    if result.market_input is not None:
        cap = result.market_input
        if "manual_market_cap_override" in cap.warnings:
            table.add_row("Market cap", "manual override (not stored market data)")
        elif cap.is_available:
            table.add_row(
                "Market cap",
                f"{cap.market_cap} {cap.currency or ''} "
                f"({cap.price_date_used}, {cap.price_provider})".strip(),
            )
        elif cap.unavailable_reason:
            table.add_row("Market cap", cap.unavailable_reason)
    console.print(table)
    if result.inputs:
        inputs = Table(title="Inputs")
        inputs.add_column("Input")
        inputs.add_column("Value", justify="right")
        for key, value in result.inputs.items():
            inputs.add_row(key, "—" if value is None else f"{value:.6g}")
        console.print(inputs)


@app.command("factors")
def factors_cmd(
    ticker: Annotated[str, typer.Argument(help="Stock ticker symbol")],
    period: Annotated[
        str,
        typer.Option("--period", help="Fiscal period, e.g. FY2023 or Q3-2023"),
    ],
    as_of_date: Annotated[
        str | None,
        typer.Option("--as-of", help="Point-in-time date (YYYY-MM-DD or ISO datetime)"),
    ] = None,
    market_cap: Annotated[
        float | None,
        typer.Option(
            "--market-cap",
            help=(
                "Optional market-cap override for valuation factors. "
                "Stored PIT market cap is used when omitted."
            ),
        ),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Calculate all registered fundamental factors for a ticker."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` and ingest first.")

    try:
        parsed_period = parse_period(period)
        as_of = _parse_as_of(as_of_date) if as_of_date else datetime.now(UTC)
    except ValueError as exc:
        _user_error(str(exc))

    db = initialize_database(path)
    try:
        with db.session() as conn:
            results = FactorEngine(conn).calculate_all(
                ticker,
                parsed_period,
                as_of=as_of,
                market_cap=market_cap,
            )
    except FinancialsError as exc:
        _user_error(str(exc))

    table = Table(title=f"Factors for {ticker.upper()} · {parsed_period.label()}")
    table.add_column("Factor")
    table.add_column("Value", justify="right")
    table.add_column("Valid")
    table.add_column("Notes")
    for result in results:
        table.add_row(
            result.factor,
            f"{result.value:.6g}" if result.value is not None else "—",
            "yes" if result.valid else "no",
            result.unavailable_reason or ("; ".join(result.warnings) if result.warnings else "—"),
        )
    console.print(table)
    console.print(f"[dim]Registered factors: {', '.join(list_factors())}[/dim]")


@app.command("rank")
def rank_cmd(
    tickers: Annotated[
        str,
        typer.Option("--tickers", help="Comma-separated tickers, e.g. AAPL,MSFT,GOOGL"),
    ],
    factor_name: Annotated[
        str,
        typer.Option("--factor", help="Factor name, e.g. revenue-growth"),
    ],
    period: Annotated[
        str,
        typer.Option("--period", help="Fiscal period, e.g. FY2023 or Q3-2023"),
    ],
    as_of_date: Annotated[
        str | None,
        typer.Option("--as-of", help="Point-in-time date (YYYY-MM-DD or ISO datetime)"),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Rank companies by a fundamental factor."""
    settings = _load_settings()
    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` and ingest first.")

    ticker_list = [part.strip().upper() for part in tickers.split(",") if part.strip()]
    if not ticker_list:
        _user_error("Provide at least one ticker via --tickers.")

    try:
        parsed_period = parse_period(period)
        as_of = _parse_as_of(as_of_date) if as_of_date else datetime.now(UTC)
    except ValueError as exc:
        _user_error(str(exc))

    db = initialize_database(path)
    try:
        with db.session() as conn:
            ranking = FactorEngine(conn).rank(
                ticker_list,
                factor_name,
                parsed_period,
                as_of=as_of,
            )
    except UnknownFactorError as exc:
        _user_error(str(exc))
    except FinancialsError as exc:
        _user_error(str(exc))

    console.print(
        Panel.fit(
            f"Factor [bold]{ranking.factor}[/bold] · {ranking.period.label()}\n"
            f"As of {ranking.as_of.astimezone(UTC).isoformat()}\n"
            f"Direction: {ranking.ranking_direction} · valid={ranking.valid_count}",
            title="rank",
        )
    )
    table = Table()
    table.add_column("Rank", justify="right")
    table.add_column("Ticker")
    table.add_column("Value", justify="right")
    table.add_column("Percentile", justify="right")
    for row in ranking.rows:
        assert row.rank is not None
        table.add_row(
            str(row.rank),
            row.result.ticker,
            f"{row.result.value:.6g}" if row.result.value is not None else "—",
            f"{row.percentile:.1f}" if row.percentile is not None else "—",
        )
    console.print(table)
    if ranking.excluded:
        excluded = Table(title="Excluded")
        excluded.add_column("Ticker")
        excluded.add_column("Reason")
        for symbol, reason in ranking.excluded:
            excluded.add_row(symbol, reason)
        console.print(excluded)


def _print_snapshot(snapshot: FinancialSnapshot) -> None:
    console.print(
        Panel.fit(
            f"[bold]{snapshot.ticker}[/bold] ({snapshot.cik}) · {snapshot.period.label()}\n"
            f"As of {snapshot.as_of.astimezone(UTC).isoformat()}\n"
            f"Period {snapshot.period_start or '—'} → {snapshot.period_end or '—'}",
            title="statements",
        )
    )
    _print_statement_table("Income statement", snapshot.income_statement)
    _print_statement_table("Balance sheet", snapshot.balance_sheet)
    _print_statement_table("Cash flow", snapshot.cash_flow)
    _print_statement_table("Derived values", snapshot.derived)

    if snapshot.source_accessions:
        console.print("[dim]Source accessions:[/dim] " + ", ".join(snapshot.source_accessions))
    if snapshot.missing:
        console.print("[yellow]Missing:[/yellow] " + ", ".join(snapshot.missing))
    if snapshot.warnings:
        console.print("[yellow]Warnings:[/yellow] " + "; ".join(snapshot.warnings))


def _print_statement_table(title: str, values: dict[str, CanonicalValue]) -> None:
    table = Table(title=title)
    table.add_column("Concept")
    table.add_column("Value", justify="right")
    table.add_column("Unit")
    table.add_column("Source concept")
    table.add_column("Accession")
    for name, item in values.items():
        table.add_row(
            name,
            f"{item.value:,.6g}" if item.value is not None else "—",
            item.unit or "—",
            item.provenance.source_concept
            or (item.provenance.derivation if item.provenance.is_derived else "—")
            or "—",
            item.provenance.accession_number or "—",
        )
    console.print(table)


def run() -> None:
    """Entrypoint used by scripts and packaging."""
    try:
        app()
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        err_console.print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    run()
