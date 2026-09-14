"""CLI commands for EquityTrace market-data (v0.3a)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from equitytrace.config import ConfigurationError, clear_settings_cache, get_settings
from equitytrace.database import initialize_database
from equitytrace.market.analytics import (
    BetaHasNoRankingDirection,
    MarketAnalyticsService,
    UnknownMarketMetricError,
)
from equitytrace.market.market_cap import MarketCapFrequency, MarketCapService
from equitytrace.market.models import AssetType, PriceAdjustmentMode
from equitytrace.market.providers.errors import MarketDataError
from equitytrace.market.service import MarketDataService
from equitytrace.repositories.market import MarketRepository

console = Console()
err_console = Console(stderr=True)

market_app = typer.Typer(
    name="market",
    help="Market-data ingestion, prices, market cap, and window analytics.",
    no_args_is_help=True,
)


def _user_error(message: str, code: int = 1) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code)


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {label} '{value}'. Use YYYY-MM-DD.") from exc


def _dec(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return format(value, "f")


@market_app.command("ingest")
def market_ingest(
    symbol: Annotated[str, typer.Argument(help="Canonical symbol, e.g. AAPL or SPY")],
    start: Annotated[str, typer.Option("--start", help="Inclusive start date YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="Inclusive end date YYYY-MM-DD")],
    provider_symbol: Annotated[
        str | None,
        typer.Option("--provider-symbol", help="Override provider symbol"),
    ] = None,
    asset_type: Annotated[
        str,
        typer.Option("--asset-type", help="equity|etf|index"),
    ] = "equity",
    raw_only: Annotated[
        bool, typer.Option("--raw-only", help="Fetch adjustment=none only")
    ] = False,
    adjusted_only: Annotated[
        bool, typer.Option("--adjusted-only", help="Fetch adjustment=all only")
    ] = False,
    overlap_days: Annotated[
        int, typer.Option("--overlap-days", help="Incremental overlap days")
    ] = 5,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Ingest daily OHLCV bars (raw and/or adjusted) for a symbol."""
    clear_settings_cache()
    settings = get_settings()
    if raw_only and adjusted_only:
        _user_error("Use only one of --raw-only or --adjusted-only.")
    if overlap_days < 0:
        _user_error("overlap-days must be >= 0.")
    try:
        start_date = _parse_date(start, "start")
        end_date = _parse_date(end, "end")
        at = AssetType(asset_type.strip().lower())
    except ValueError as exc:
        _user_error(str(exc))
    if start_date > end_date:
        _user_error(f"start_date {start_date} is after end_date {end_date}.")
    modes = [PriceAdjustmentMode.NONE, PriceAdjustmentMode.ALL]
    if raw_only:
        modes = [PriceAdjustmentMode.NONE]
    if adjusted_only:
        modes = [PriceAdjustmentMode.ALL]

    path = database or settings.database_path
    db = initialize_database(path)
    with db.session() as conn:
        service = MarketDataService(conn, settings)
        try:
            result = service.ingest(
                symbol,
                start_date=start_date,
                end_date=end_date,
                asset_type=at,
                provider_symbol=provider_symbol,
                modes=modes,
                overlap_days=overlap_days,
            )
        except (MarketDataError, ConfigurationError) as exc:
            _user_error(str(exc))

    table = Table(title="Market ingest", show_header=False)
    table.add_row("Instrument", result.canonical_symbol)
    table.add_row("Provider", result.provider.value)
    table.add_row("Provider symbol", result.provider_symbol)
    table.add_row(
        "Requested range",
        f"{result.requested_start_date} → {result.requested_end_date}",
    )
    table.add_row(
        "Adjustment modes",
        ", ".join(m.value for m in result.adjustment_modes),
    )
    table.add_row("Raw rows", str(result.raw_row_count))
    table.add_row("Inserted", str(result.inserted_row_count))
    table.add_row("Updated", str(result.updated_row_count))
    table.add_row("Unchanged", str(result.unchanged_row_count))
    table.add_row("Rejected", str(result.rejected_row_count))
    table.add_row(
        "Stored range",
        f"{result.stored_start_date or '—'} → {result.stored_end_date or '—'}",
    )
    table.add_row("Run status", result.status.value)
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow] " + "; ".join(result.warnings))
    if result.status.value == "failed":
        _user_error(
            result.error_summary
            or f"Market ingest failed for {result.canonical_symbol} "
            f"(rejected={result.rejected_row_count}, inserted={result.inserted_row_count})."
        )
    if result.error_summary:
        _user_error(result.error_summary)


@market_app.command("prices")
def market_prices(
    symbol: Annotated[str, typer.Argument(help="Canonical symbol")],
    start: Annotated[str, typer.Option("--start", help="Inclusive start date")],
    end: Annotated[str, typer.Option("--end", help="Inclusive end date")],
    adjustment: Annotated[
        str,
        typer.Option("--adjustment", help="none|all"),
    ] = "none",
    limit: Annotated[int | None, typer.Option("--limit", help="Max rows")] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="table|json"),
    ] = "table",
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """List stored daily price bars."""
    clear_settings_cache()
    settings = get_settings()
    try:
        start_date = _parse_date(start, "start")
        end_date = _parse_date(end, "end")
        mode = PriceAdjustmentMode(adjustment.strip().lower())
    except ValueError as exc:
        _user_error(str(exc))
    if start_date > end_date:
        _user_error(f"start_date {start_date} is after end_date {end_date}.")
    if limit is not None and limit <= 0:
        _user_error("--limit must be a positive integer.")
    if output_format not in {"table", "json"}:
        _user_error("--format must be table or json.")

    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    db = initialize_database(path)
    with db.session(read_only=True) as conn:
        repo = MarketRepository(conn)
        instrument = repo.get_instrument_by_symbol(symbol)
        if instrument is None:
            _user_error(
                f"No market instrument for {symbol.upper()}. Run `equitytrace market ingest` first."
            )
            return
        bars = repo.get_price_bars(
            instrument.instrument_id,
            start_date=start_date,
            end_date=end_date,
            adjustment_mode=mode,
            limit=limit,
        )

    if output_format == "json":
        payload = [
            {
                "trading_date": b.trading_date.isoformat(),
                "adjustment_mode": b.adjustment_mode.value,
                "open": str(b.open),
                "high": str(b.high),
                "low": str(b.low),
                "close": str(b.close),
                "volume": b.volume,
                "currency": b.currency,
                "available_at": b.available_at.isoformat(),
                "fetched_at": b.fetched_at.isoformat(),
                "provider": b.provider.value,
            }
            for b in bars
        ]
        console.print_json(json.dumps(payload))
        return

    table = Table(title=f"Prices {symbol.upper()} · {mode.value}")
    table.add_column("Date")
    table.add_column("Open", justify="right")
    table.add_column("High", justify="right")
    table.add_column("Low", justify="right")
    table.add_column("Close", justify="right")
    table.add_column("Volume", justify="right")
    for b in bars:
        table.add_row(
            b.trading_date.isoformat(),
            _dec(b.open),
            _dec(b.high),
            _dec(b.low),
            _dec(b.close),
            str(b.volume),
        )
    console.print(table)


@market_app.command("cap")
def market_cap_cmd(
    symbol: Annotated[str, typer.Argument(help="Canonical symbol")],
    market_date: Annotated[str, typer.Option("--date", help="Market date YYYY-MM-DD")],
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="Optional later knowledge time (ISO datetime)"),
    ] = None,
    max_price_staleness_days: Annotated[
        int,
        typer.Option("--max-price-staleness-days", help="Max calendar days price may lag"),
    ] = 7,
    output_format: Annotated[
        str,
        typer.Option("--format", help="table|json"),
    ] = "table",
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Compute historically safe market capitalization for one date."""
    clear_settings_cache()
    settings = get_settings()
    try:
        day = _parse_date(market_date, "date")
        knowledge = None
        if as_of:
            parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            knowledge = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError as exc:
        _user_error(str(exc))
    if max_price_staleness_days < 0:
        _user_error("--max-price-staleness-days must be >= 0.")
    if output_format not in {"table", "json"}:
        _user_error("--format must be table or json.")

    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    db = initialize_database(path)
    with db.session(read_only=True) as conn:
        result = MarketCapService(conn).get_market_cap(
            symbol,
            day,
            as_of=knowledge,
            max_price_staleness_days=max_price_staleness_days,
        )

    if output_format == "json":
        console.print_json(json.dumps(_cap_to_dict(result)))
        return

    table = Table(title=f"Market cap · {symbol.upper()}", show_header=False)
    for key, value in _cap_to_dict(result).items():
        table.add_row(key, "—" if value is None else str(value))
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow] " + "; ".join(result.warnings))
    if not result.is_available:
        console.print(
            Panel(
                result.unavailable_reason or "unavailable",
                title="Unavailable",
                style="yellow",
            )
        )


@market_app.command("cap-series")
def market_cap_series_cmd(
    symbol: Annotated[str, typer.Argument(help="Canonical symbol")],
    start: Annotated[str, typer.Option("--start", help="Inclusive start date")],
    end: Annotated[str, typer.Option("--end", help="Inclusive end date")],
    frequency: Annotated[
        str,
        typer.Option("--frequency", help="daily|month-end|quarter-end"),
    ] = "month-end",
    output_format: Annotated[
        str,
        typer.Option("--format", help="table|json"),
    ] = "table",
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Compute a historical market-cap series."""
    clear_settings_cache()
    settings = get_settings()
    try:
        start_date = _parse_date(start, "start")
        end_date = _parse_date(end, "end")
        freq = MarketCapFrequency(frequency.strip().lower())
    except ValueError as exc:
        _user_error(str(exc))
    if start_date > end_date:
        _user_error(f"start_date {start_date} is after end_date {end_date}.")
    if output_format not in {"table", "json"}:
        _user_error("--format must be table or json.")

    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    db = initialize_database(path)
    with db.session(read_only=True) as conn:
        points = MarketCapService(conn).get_market_cap_series(
            symbol,
            start_date,
            end_date,
            frequency=freq,
        )

    if output_format == "json":
        payload = [
            {
                "as_of_date": p.as_of_date.isoformat(),
                **_cap_to_dict(p.result),
            }
            for p in points
        ]
        console.print_json(json.dumps(payload))
        return

    table = Table(title=f"Market-cap series · {symbol.upper()} · {freq.value}")
    table.add_column("Date")
    table.add_column("Price date")
    table.add_column("Raw close", justify="right")
    table.add_column("Shares", justify="right")
    table.add_column("Market cap", justify="right")
    table.add_column("Status")
    for p in points:
        r = p.result
        table.add_row(
            p.as_of_date.isoformat(),
            r.price_date_used.isoformat() if r.price_date_used else "—",
            _dec(r.raw_close),
            _dec(r.shares_outstanding),
            _dec(r.market_cap),
            "ok" if r.is_available else (r.unavailable_reason or "unavailable"),
        )
    console.print(table)


@market_app.command("analytics")
def market_analytics_cmd(
    symbol: Annotated[str, typer.Argument(help="Canonical symbol, e.g. AAPL")],
    as_of_date: Annotated[
        str,
        typer.Option("--as-of", help="Point-in-time date (YYYY-MM-DD or ISO datetime)"),
    ],
    benchmark: Annotated[
        str,
        typer.Option("--benchmark", help="Stored benchmark symbol for beta"),
    ] = "SPY",
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Show stored-price momentum, volatility, beta, and max drawdown."""
    clear_settings_cache()
    settings = get_settings()
    try:
        as_of = _parse_as_of(as_of_date)
    except ValueError as exc:
        _user_error(str(exc))

    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    db = initialize_database(path)
    with db.session(read_only=True) as conn:
        results = MarketAnalyticsService(conn).calculate_all(
            symbol,
            as_of,
            benchmark=benchmark,
        )

    table = Table(title=f"Market analytics · {symbol.upper()}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_column("Window")
    table.add_column("Notes")
    for result in results:
        window = "—"
        if result.window_start and result.window_end:
            window = f"{result.window_start} → {result.window_end}"
        notes = result.unavailable_reason or "; ".join(result.warnings) or "—"
        table.add_row(
            result.metric,
            f"{result.value:.6g}" if result.value is not None else "—",
            window,
            notes,
        )
    console.print(table)
    if results and results[0].warnings:
        console.print("[dim]" + "; ".join(results[0].warnings) + "[/dim]")


@market_app.command("rank-metric")
def market_rank_metric_cmd(
    metric: Annotated[str, typer.Argument(help="Market metric name, e.g. momentum_12_1")],
    tickers: Annotated[list[str], typer.Argument(help="Tickers to rank")],
    as_of_date: Annotated[
        str,
        typer.Option("--as-of", help="Point-in-time date (YYYY-MM-DD or ISO datetime)"),
    ],
    benchmark: Annotated[
        str,
        typer.Option("--benchmark", help="Stored benchmark symbol for beta"),
    ] = "SPY",
    database: Annotated[
        Path | None,
        typer.Option("--database", help="DuckDB database path override."),
    ] = None,
) -> None:
    """Rank tickers by a market-window metric. Beta ranking is refused."""
    clear_settings_cache()
    settings = get_settings()
    symbols = [
        part.strip().upper() for ticker in tickers for part in ticker.split(",") if part.strip()
    ]
    if not symbols:
        _user_error("Provide at least one ticker.")
    try:
        as_of = _parse_as_of(as_of_date)
    except ValueError as exc:
        _user_error(str(exc))

    path = database or settings.database_path
    if not path.exists():
        _user_error(f"Database not found at {path}. Run `equitytrace init-db` first.")
    db = initialize_database(path)
    try:
        with db.session(read_only=True) as conn:
            ranking = MarketAnalyticsService(conn).rank(
                symbols,
                metric,
                as_of,
                benchmark=benchmark,
            )
    except (UnknownMarketMetricError, BetaHasNoRankingDirection) as exc:
        _user_error(str(exc))

    console.print(
        Panel.fit(
            f"Metric [bold]{ranking.metric}[/bold]\n"
            f"As of {ranking.as_of.astimezone(UTC).isoformat()}\n"
            f"Direction: {ranking.ranking_direction} · valid={ranking.valid_count}",
            title="rank-metric",
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


def _parse_as_of(value: str) -> datetime:
    text = value.strip()
    if "T" in text or " " in text:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    day = date.fromisoformat(text)
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


def _cap_to_dict(result: Any) -> dict[str, Any]:
    return {
        "ticker": result.ticker,
        "market_date_requested": result.market_date_requested.isoformat(),
        "price_date_used": result.price_date_used.isoformat() if result.price_date_used else None,
        "knowledge_time": result.knowledge_time.isoformat() if result.knowledge_time else None,
        "raw_close": str(result.raw_close) if result.raw_close is not None else None,
        "currency": result.currency,
        "shares_outstanding": str(result.shares_outstanding)
        if result.shares_outstanding is not None
        else None,
        "shares_fact_date": result.shares_fact_date.isoformat()
        if result.shares_fact_date
        else None,
        "shares_available_at": result.shares_available_at.isoformat()
        if result.shares_available_at
        else None,
        "market_cap": str(result.market_cap) if result.market_cap is not None else None,
        "price_provider": result.price_provider.value if result.price_provider else None,
        "price_adjustment_mode": result.price_adjustment_mode.value
        if result.price_adjustment_mode
        else None,
        "price_fetched_at": result.price_fetched_at.isoformat()
        if result.price_fetched_at
        else None,
        "sec_concept": result.sec_concept,
        "sec_accession": result.sec_accession,
        "sec_form": result.sec_form,
        "warnings": list(result.warnings),
        "unavailable_reason": result.unavailable_reason,
    }
