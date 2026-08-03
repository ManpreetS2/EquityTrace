"""FilingEdge command-line interface."""

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

from filingedge import __version__
from filingedge.config import ConfigurationError, Settings, clear_settings_cache, get_settings
from filingedge.database import Database, initialize_database
from filingedge.repositories.facts import FactsRepository
from filingedge.repositories.filings import FilingsRepository
from filingedge.repositories.ingestion import IngestionError, IngestionRepository
from filingedge.repositories.securities import SecuritiesRepository
from filingedge.sec.client import SecClient, SecClientError, SecHttpError
from filingedge.sec.tickers import resolve_ticker

app = typer.Typer(
    name="filingedge",
    help="FilingEdge: point-in-time SEC filing ingestion and fundamental data.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


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
    """FilingEdge CLI."""
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
        _user_error(f"Database not found at {path}. Run `filingedge init-db` and ingest first.")

    db = Database(path)
    with db.session(read_only=True) as conn:
        if SecuritiesRepository(conn).resolve_cik(ticker) is None:
            _user_error(
                f"No stored data for ticker {ticker.upper()}. Run `filingedge ingest` first."
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
        _user_error(f"Database not found at {path}. Run `filingedge init-db` and ingest first.")

    db = Database(path)
    with db.session(read_only=True) as conn:
        if SecuritiesRepository(conn).resolve_cik(ticker) is None:
            _user_error(
                f"No stored data for ticker {ticker.upper()}. Run `filingedge ingest` first."
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
        _user_error(f"Database not found at {path}. Run `filingedge init-db` and ingest first.")

    try:
        as_of = _parse_as_of(as_of_date)
    except ValueError as exc:
        _user_error(str(exc))

    db = Database(path)
    with db.session(read_only=True) as conn:
        if SecuritiesRepository(conn).resolve_cik(ticker) is None:
            _user_error(
                f"No stored data for ticker {ticker.upper()}. Run `filingedge ingest` first."
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
        _user_error(f"Database not found at {path}. Run `filingedge init-db` first.")

    db = Database(path)
    stats = IngestionRepository(db).database_stats()
    latest = stats["latest_successful_ingestion"]

    table = Table(title="FilingEdge Database", show_header=False)
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
    """Print the FilingEdge version."""
    console.print(__version__)


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
