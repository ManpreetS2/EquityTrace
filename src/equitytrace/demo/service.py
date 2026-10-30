"""UI-free service boundary for the synthetic research demo.

Every research number comes from the existing engines (``FinancialsService``,
``FactorEngine``, ``BacktestEngine``, ``PortfolioRepository`` and the reporting
module). This layer only opens short-lived, explicitly owned DuckDB sessions,
serialises access inside the process, and shapes results for presentation.

Only :meth:`DemoService.prepare` and :meth:`DemoService.run_strategy` write.
Nothing else, including rendering a saved run, can execute research.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
from pydantic import ValidationError

from equitytrace.database import Database
from equitytrace.demo.data import (
    BENCHMARK_SYMBOL,
    CALENDAR_SYMBOL,
    COMPANIES,
    DATASET_ID,
    DATASET_VERSION,
    PRICE_END,
    PRICE_START,
)
from equitytrace.demo.prepare import (
    DemoPaths,
    PrepareOutcome,
    prepare_demo,
    read_manifest,
)
from equitytrace.factors.engine import FactorEngine
from equitytrace.factors.models import FactorResult
from equitytrace.factors.ranking import rank_factor_results
from equitytrace.factors.registry import get_factor, list_factors
from equitytrace.financials.models import FinancialPeriod, FinancialSnapshot
from equitytrace.financials.service import FinancialsError, FinancialsService
from equitytrace.market.availability import bar_available_at
from equitytrace.models import Filing
from equitytrace.portfolio.calendar import (
    CalendarError,
    resolve_rebalance_pairs,
    sessions_from_bars,
)
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.portfolio.models import (
    BacktestRequest,
    BacktestResult,
)
from equitytrace.portfolio.signals import resolve_latest_annual_factor
from equitytrace.reporting.export import render_request_export, render_result_export
from equitytrace.reporting.html import render_research_report
from equitytrace.repositories.filings import FilingsRepository
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.portfolio import PortfolioRepository
from equitytrace.strategy.models import (
    ResearchStrategy,
    fingerprint_request,
    fingerprint_strategy,
)

SYNTHETIC_REPORT_BANNER = (
    "SYNTHETIC DEMO ONLY — not live SEC/Twelve Data, not historical investment "
    "performance, and not investment advice."
)
_DB_LOCK = threading.RLock()
_MAX_TOKENS = 128
_RUN_TOKENS: OrderedDict[str, str] = OrderedDict()
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
EASTERN_NAME = "America/New_York"


def default_demo_directory() -> Path:
    """``$EQUITYTRACE_DEMO_DIR`` or ``./data/demo`` (``data/`` is gitignored)."""
    configured = os.environ.get("EQUITYTRACE_DEMO_DIR", "").strip()
    return Path(configured) if configured else Path.cwd() / "data" / "demo"


def label_synthetic_report(html: str) -> str:
    """Stamp a standalone report so it cannot be mistaken for real results."""
    marker = '<main class="page">\n'
    if marker not in html:
        raise RuntimeError("Report layout changed; cannot place the synthetic-demo label.")
    banner = (
        '<p role="note" style="border:2px solid #b45309;background:#fef3c7;color:#451a03;'
        f'padding:12px;font-weight:bold">{SYNTHETIC_REPORT_BANNER}</p>\n'
    )
    return html.replace(marker, marker + banner, 1)


def decision_time(day: date) -> datetime:
    """The engine's decision convention: the day's bar becomes public at 16:15 Eastern."""
    return bar_available_at(day, EASTERN_NAME)


@dataclass(frozen=True, slots=True)
class DemoStatus:
    prepared: bool
    message: str
    directory: Path
    database: Path
    dataset_id: str = DATASET_ID
    dataset_version: str = DATASET_VERSION
    content_digest: str | None = None
    tickers: tuple[str, ...] = ()
    fact_count: int = 0
    bar_count: int = 0
    price_start: date = PRICE_START
    price_end: date = PRICE_END
    saved_run_count: int = 0


@dataclass(frozen=True, slots=True)
class SecurityRow:
    ticker: str
    issuer_name: str
    cik: str
    exchange: str | None
    is_primary: bool
    securities_for_issuer: int


@dataclass(frozen=True, slots=True)
class FilingRow:
    accession: str
    form: str
    filing_date: date
    report_date: date | None
    available_at: datetime
    public_at_as_of: bool
    source_url: str | None


@dataclass(frozen=True, slots=True)
class StrategyPreset:
    key: str
    title: str
    summary: str
    expectation: str  # "success" | "unavailable"
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    strategy: ResearchStrategy | None = None
    canonical_json: str | None = None
    fingerprint: str | None = None
    ranking_direction: str | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    status: str
    created_at: datetime
    factor: str
    baseline: str
    top_n: int
    start_date: date
    end_date: date
    final_nav: float | None
    failure_reason: str | None
    fingerprint: str | None


@dataclass(frozen=True, slots=True)
class AvailabilityProbe:
    probe_time: datetime | None
    rows: tuple[tuple[str, bool, str], ...]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RankedName:
    ticker: str
    rank: int | None
    result: FactorResult | None
    reason: str | None


@dataclass(slots=True)
class SnapshotView:
    ticker: str
    period: FinancialPeriod
    as_of: datetime
    snapshot: FinancialSnapshot
    factors: list[FactorResult] = field(default_factory=list)


class DemoNotPreparedError(RuntimeError):
    """The demo database has not been prepared (or failed verification)."""


def _demo_payload(
    *,
    name: str,
    description: str,
    tickers: list[str],
    factor: str,
    top_n: int,
    baseline: str,
    schedule: str,
    start: str,
    end: str,
    explicit_dates: list[str] | None = None,
    cost_bps: float = 10.0,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "tickers": tickers,
        "factor": factor,
        "top_n": top_n,
        "baseline": baseline,
        "start_date": start,
        "end_date": end,
        "schedule": schedule,
        "explicit_dates": explicit_dates or [],
        "calendar_symbol": CALENDAR_SYMBOL,
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "cost_bps": cost_bps,
        "initial_nav": 1.0,
        "provider": "twelve_data",
        "adjustment_mode": "all",
    }


# One ticker per issuer. SYGB is the second class of the SYGA issuer.
WINDOW_START = "2023-07-03"
WINDOW_END = "2024-12-31"
SINGLE_CLASS_UNIVERSE = ["SYAL", "SYBR", "SYCD", "SYDL", "SYEN", "SYFM", "SYGA"]
_SYNTH = "SYNTHETIC DEMO ONLY. Fictional issuers; not investment advice. "


def build_presets() -> tuple[StrategyPreset, ...]:
    return (
        StrategyPreset(
            key="roa-equal-monthly",
            title="ROA quality · top 3 · equal weight · monthly",
            summary=(
                "Ranks the seven names by return on assets, holds the top three at equal "
                "weight, and rebalances at each month end."
            ),
            expectation="success",
            payload=_demo_payload(
                name="Synthetic ROA equal-weight monthly",
                description=_SYNTH + "Return on assets, top 3, equal weight, monthly.",
                tickers=SINGLE_CLASS_UNIVERSE,
                factor="roa",
                top_n=3,
                baseline="equal_weight",
                schedule="monthly",
                start=WINDOW_START,
                end=WINDOW_END,
            ),
        ),
        StrategyPreset(
            key="growth-inverse-vol-quarterly",
            title="Revenue growth · top 3 · inverse volatility · quarterly",
            summary=(
                "Ranks by year-over-year revenue growth and weights the top three by inverse "
                "volatility of the previous 252 daily returns."
            ),
            expectation="success",
            payload=_demo_payload(
                name="Synthetic revenue-growth inverse-vol quarterly",
                description=_SYNTH + "Revenue growth, top 3, inverse volatility, quarterly.",
                tickers=SINGLE_CLASS_UNIVERSE,
                factor="revenue_growth",
                top_n=3,
                baseline="inverse_vol",
                schedule="quarterly",
                start=WINDOW_START,
                end=WINDOW_END,
            ),
        ),
        StrategyPreset(
            key="margin-min-variance-monthly",
            title="Operating-margin change · top 3 · minimum variance · monthly",
            summary=(
                "Ranks by the change in operating margin and sets the top three weights with "
                "the skfolio minimum-variance adapter (no fallback if it fails)."
            ),
            expectation="success",
            payload=_demo_payload(
                name="Synthetic operating-margin minimum-variance monthly",
                description=_SYNTH + "Operating-margin change, top 3, minimum variance, monthly.",
                tickers=SINGLE_CLASS_UNIVERSE,
                factor="operating_margin_change",
                top_n=3,
                baseline="min_variance",
                schedule="monthly",
                start=WINDOW_START,
                end=WINDOW_END,
            ),
        ),
        StrategyPreset(
            key="diag-share-classes",
            title="Diagnostic · two share classes of one issuer",
            summary=(
                "Includes SYGA and SYGB, which are one issuer. The engine refuses instead of "
                "silently picking a share class."
            ),
            expectation="unavailable",
            payload=_demo_payload(
                name="Diagnostic: ambiguous share classes",
                description=_SYNTH + "Expected UNAVAILABLE: SYGA and SYGB share one issuer.",
                tickers=[*SINGLE_CLASS_UNIVERSE, "SYGB"],
                factor="roa",
                top_n=3,
                baseline="equal_weight",
                schedule="monthly",
                start=WINDOW_START,
                end=WINDOW_END,
            ),
        ),
        StrategyPreset(
            key="diag-valuation-no-market-cap",
            title="Diagnostic · valuation factor without share counts",
            summary=(
                "P/E needs split-safe market capitalization. The demo stores no share counts, "
                "so the factor is unavailable rather than guessed."
            ),
            expectation="unavailable",
            payload=_demo_payload(
                name="Diagnostic: valuation without market cap",
                description=_SYNTH + "Expected UNAVAILABLE: no share counts for market cap.",
                tickers=SINGLE_CLASS_UNIVERSE,
                factor="price_to_earnings",
                top_n=3,
                baseline="equal_weight",
                schedule="monthly",
                start=WINDOW_START,
                end=WINDOW_END,
            ),
        ),
        StrategyPreset(
            key="diag-short-price-history",
            title="Diagnostic · optimizer lacks price history",
            summary=(
                "Inverse volatility needs 253 common closes, but the first decision falls "
                "only about half a year into the price history."
            ),
            expectation="unavailable",
            payload=_demo_payload(
                name="Diagnostic: insufficient optimizer history",
                description=_SYNTH + "Expected UNAVAILABLE: fewer than 253 aligned closes.",
                tickers=SINGLE_CLASS_UNIVERSE,
                factor="roa",
                top_n=3,
                baseline="inverse_vol",
                schedule="monthly",
                start="2023-01-03",
                end="2023-04-28",
            ),
        ),
        StrategyPreset(
            key="diag-evidence-not-public",
            title="Diagnostic · prior-year evidence not yet available",
            summary=(
                "At the first decision the newest public annual report is FY2021, and revenue "
                "growth needs FY2020 revenue, which does not exist in the dataset."
            ),
            expectation="unavailable",
            payload=_demo_payload(
                name="Diagnostic: evidence not yet public",
                description=_SYNTH + "Expected UNAVAILABLE: no FY2020 revenue evidence.",
                tickers=SINGLE_CLASS_UNIVERSE,
                factor="revenue_growth",
                top_n=3,
                baseline="equal_weight",
                schedule="explicit",
                start="2023-01-03",
                end="2023-02-28",
                explicit_dates=["2023-01-31"],
            ),
        ),
    )


class DemoService:
    """Short-lived, lock-serialised access to the demo database."""

    def __init__(self, directory: Path | None = None) -> None:
        self.paths = DemoPaths((directory or default_demo_directory()).resolve())

    # ------------------------------------------------------------------ status
    @contextmanager
    def _session(self, *, read_only: bool) -> Iterator[duckdb.DuckDBPyConnection]:
        with _DB_LOCK:
            if not self.paths.database.exists():
                raise DemoNotPreparedError("The demo database has not been prepared yet.")
            db = Database(self.paths.database)
            with db.session(read_only=read_only) as conn:
                conn.execute("SET TimeZone='UTC'")
                yield conn

    def status(self) -> DemoStatus:
        """Cheap readiness check: manifest marker plus stored row counts."""
        paths = self.paths
        manifest = read_manifest(paths)
        if manifest is None or not paths.database.exists():
            return DemoStatus(
                prepared=False,
                message="The synthetic demo has not been prepared yet.",
                directory=paths.directory,
                database=paths.database,
            )
        if manifest.get("dataset_version") != DATASET_VERSION:
            return DemoStatus(
                prepared=False,
                message="The stored demo comes from a different dataset version; rebuild it.",
                directory=paths.directory,
                database=paths.database,
            )
        try:
            with self._session(read_only=True) as conn:
                facts = _scalar(conn, "SELECT count(*) FROM financial_facts")
                bars = _scalar(conn, "SELECT count(*) FROM daily_price_bars")
                runs = _scalar(conn, "SELECT count(*) FROM portfolio_runs")
        except (duckdb.Error, DemoNotPreparedError) as exc:
            return DemoStatus(
                prepared=False,
                message=f"The demo database cannot be read: {exc}",
                directory=paths.directory,
                database=paths.database,
            )
        if facts != manifest.get("fact_count") or bars != manifest.get("bar_count"):
            return DemoStatus(
                prepared=False,
                message="Stored row counts do not match the demo manifest; rebuild the demo.",
                directory=paths.directory,
                database=paths.database,
            )
        return DemoStatus(
            prepared=True,
            message="Synthetic demo is prepared.",
            directory=paths.directory,
            database=paths.database,
            content_digest=str(manifest.get("content_digest")),
            tickers=tuple(str(t) for t in manifest.get("tickers", [])),
            fact_count=facts,
            bar_count=bars,
            saved_run_count=runs,
        )

    def prepare(self, *, overwrite: bool = False) -> PrepareOutcome:
        """Create or verify the demo database. The only data-preparing action."""
        with _DB_LOCK:
            return prepare_demo(self.paths.directory, overwrite=overwrite)

    # ---------------------------------------------------------------- explorer
    def securities(self) -> list[SecurityRow]:
        with self._session(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT s.ticker, i.legal_name, s.cik, s.exchange, s.is_primary,
                       count(*) OVER (PARTITION BY s.cik) AS siblings
                FROM securities s JOIN issuers i ON i.cik = s.cik
                ORDER BY s.ticker
                """
            ).fetchall()
        return [
            SecurityRow(
                ticker=str(r[0]),
                issuer_name=str(r[1]),
                cik=str(r[2]),
                exchange=None if r[3] is None else str(r[3]),
                is_primary=bool(r[4]),
                securities_for_issuer=int(r[5]),
            )
            for r in rows
        ]

    def company_story(self, ticker: str) -> str | None:
        for spec in COMPANIES:
            if ticker in (spec.ticker, *spec.extra_tickers):
                return f"{spec.sector}. {spec.story}"
        return None

    def available_periods(self, ticker: str, as_of: datetime) -> list[FinancialPeriod]:
        with self._session(read_only=True) as conn:
            return FinancialsService(conn).list_available_annual_periods(ticker, as_of)

    def snapshot_view(
        self, ticker: str, fiscal_year: int, as_of: datetime, *, with_factors: bool = True
    ) -> SnapshotView:
        period = FinancialPeriod(fiscal_year=fiscal_year, fiscal_period="FY", kind="annual")
        with self._session(read_only=True) as conn:
            snapshot = FinancialsService(conn).get_snapshot(ticker, period, as_of=as_of)
            factors: list[FactorResult] = []
            if with_factors:
                engine = FactorEngine(conn)
                for name in list_factors():
                    try:
                        factors.append(
                            engine.calculate(ticker, name, period, as_of=as_of, persist=False)
                        )
                    except FinancialsError:
                        continue
        return SnapshotView(ticker, period, as_of, snapshot, factors)

    def filings(self, ticker: str, as_of: datetime) -> list[FilingRow]:
        with self._session(read_only=True) as conn:
            filings: list[Filing] = FilingsRepository(conn).list_for_ticker(ticker, limit=50)
        return [
            FilingRow(
                accession=f.accession_number,
                form=f.form,
                filing_date=f.filing_date,
                report_date=f.report_date,
                available_at=f.available_at,
                public_at_as_of=f.available_at <= as_of,
                source_url=f.source_url,
            )
            for f in sorted(filings, key=lambda item: item.available_at)
        ]

    def filings_by_accession(self, accessions: tuple[str, ...]) -> list[FilingRow]:
        if not accessions:
            return []
        placeholders = ", ".join("?" for _ in accessions)
        with self._session(read_only=True) as conn:
            rows = conn.execute(
                f"SELECT accession_number, form, filing_date, report_date, available_at, "
                f"source_url FROM filings WHERE accession_number IN ({placeholders}) "
                f"ORDER BY available_at, accession_number",
                list(accessions),
            ).fetchall()
        return [
            FilingRow(
                str(r[0]), str(r[1]), r[2], r[3], r[4], True, None if r[5] is None else str(r[5])
            )
            for r in rows
        ]

    def ranking_at(self, tickers: list[str], factor: str, as_of: datetime) -> list[RankedName]:
        """Cross-section as the backtest sees it: each name's newest public annual period."""
        with self._session(read_only=True) as conn:
            engine = FactorEngine(conn)
            valid: list[FactorResult] = []
            reasons: dict[str, str] = {}
            for ticker in tickers:
                result, reason, _known = resolve_latest_annual_factor(engine, ticker, factor, as_of)
                if result is None:
                    reasons[ticker] = reason or "factor unavailable"
                else:
                    valid.append(result)
        names: list[RankedName] = []
        if valid:
            ranking = rank_factor_results(
                results=valid,
                excluded=[],
                factor=factor,
                period=valid[0].period,
                as_of=as_of,
                ranking_direction=get_factor(factor).ranking_direction,
            )
            names.extend(RankedName(r.result.ticker, r.rank, r.result, None) for r in ranking.rows)
        names.extend(RankedName(t, None, None, why) for t, why in sorted(reasons.items()))
        return names

    # --------------------------------------------------------------- strategy
    def presets(self) -> tuple[StrategyPreset, ...]:
        return build_presets()

    def validate_strategy(self, payload: Mapping[str, Any]) -> ValidationOutcome:
        """Validate with the existing ``ResearchStrategy`` model; never touches the engine."""
        try:
            strategy = ResearchStrategy.model_validate(dict(payload))
        except ValidationError as exc:
            errors = tuple(_format_error(error) for error in exc.errors())
            return ValidationOutcome(ok=False, errors=errors)
        known = set(self._known_symbols())
        warnings = [
            f"{symbol} is not in the demo dataset; a run would be unavailable."
            for symbol in strategy.tickers
            if known and symbol not in known
        ]
        for label, symbol in (
            ("calendar_symbol", strategy.calendar_symbol),
            ("benchmark_symbol", strategy.benchmark_symbol),
        ):
            if symbol and known and symbol not in known:
                warnings.append(f"{label} {symbol} is not in the demo dataset.")
        if strategy.start_date < PRICE_START or strategy.end_date > PRICE_END:
            warnings.append(
                f"The demo prices cover {PRICE_START.isoformat()} to {PRICE_END.isoformat()}."
            )
        return ValidationOutcome(
            ok=True,
            errors=(),
            warnings=tuple(warnings),
            strategy=strategy,
            canonical_json=strategy_pretty_json(strategy),
            fingerprint=fingerprint_strategy(strategy),
            ranking_direction=get_factor(strategy.factor).ranking_direction,
        )

    def _known_symbols(self) -> list[str]:
        try:
            with self._session(read_only=True) as conn:
                return [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT canonical_symbol FROM market_instruments"
                    ).fetchall()
                ]
        except (DemoNotPreparedError, duckdb.Error):
            return []

    def run_strategy(self, strategy: ResearchStrategy, *, token: str) -> BacktestResult:
        """Execute one backtest. ``token`` makes a repeated submission a no-op.

        A token that already produced a run returns that persisted run instead of
        running again, so a double click cannot create a duplicate run.
        """
        request = strategy.to_backtest_request()
        with _DB_LOCK:
            existing = _RUN_TOKENS.get(token)
            if existing is not None:
                loaded = self.load_run(existing)
                if loaded is not None:
                    return loaded
            with self._session(read_only=False) as conn:
                result = BacktestEngine(conn).run(request)
                loaded = PortfolioRepository(conn).get_run(result.run_id)
            if loaded is None:
                raise RuntimeError("The run was not persisted; refusing to display it.")
            _RUN_TOKENS[token] = loaded.run_id
            while len(_RUN_TOKENS) > _MAX_TOKENS:
                _RUN_TOKENS.popitem(last=False)
            return loaded

    def probe_availability(self, request: BacktestRequest) -> AvailabilityProbe:
        """Explain an unavailable run: factor validity per ticker at the first decision."""
        try:
            with self._session(read_only=True) as conn:
                market = MarketRepository(conn)
                instruments = market.get_instruments_by_symbols([request.calendar_symbol])
                calendar = instruments.get(request.calendar_symbol)
                if calendar is None:
                    return AvailabilityProbe(None, (), "The calendar symbol has no stored prices.")
                bars = market.get_price_bars_for_instruments(
                    [calendar.instrument_id],
                    start_date=request.start_date,
                    end_date=request.end_date,
                    adjustment_mode=request.adjustment_mode,
                    provider=request.provider,
                )
                try:
                    pairs = resolve_rebalance_pairs(
                        sessions_from_bars(bars),
                        schedule=request.schedule,
                        start_date=request.start_date,
                        end_date=request.end_date,
                        explicit_dates=request.explicit_dates,
                    )
                except CalendarError as exc:
                    return AvailabilityProbe(None, (), f"Calendar: {exc.reason}")
                probe_time = pairs[0][0].available_at
                engine = FactorEngine(conn)
                rows: list[tuple[str, bool, str]] = []
                for ticker in request.tickers:
                    result, reason, _known = resolve_latest_annual_factor(
                        engine, ticker, request.factor, probe_time
                    )
                    if result is None:
                        rows.append((ticker, False, reason or "factor unavailable"))
                    else:
                        rows.append(
                            (ticker, True, f"{result.period.label()} value {result.value:.4f}")
                        )
        except (DemoNotPreparedError, duckdb.Error) as exc:
            return AvailabilityProbe(None, (), f"Probe unavailable: {exc}")
        return AvailabilityProbe(probe_time, tuple(rows))

    # ----------------------------------------------------------- saved results
    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        try:
            with self._session(read_only=True) as conn:
                rows = conn.execute(
                    "SELECT run_id, status, created_at, factor_name, baseline, top_n, start_date, "
                    "end_date, final_nav, failure_reason, request_json FROM portfolio_runs "
                    "ORDER BY created_at DESC, run_id LIMIT ?",
                    [limit],
                ).fetchall()
        except (DemoNotPreparedError, duckdb.Error):
            return []
        summaries: list[RunSummary] = []
        for r in rows:
            try:
                fingerprint = fingerprint_request(BacktestRequest.model_validate_json(str(r[10])))
            except ValueError:
                fingerprint = None
            summaries.append(
                RunSummary(
                    run_id=str(r[0]),
                    status=str(r[1]),
                    created_at=r[2],
                    factor=str(r[3]),
                    baseline=str(r[4]),
                    top_n=int(r[5]),
                    start_date=r[6],
                    end_date=r[7],
                    final_nav=None if r[8] is None else float(r[8]),
                    failure_reason=None if r[9] is None else str(r[9]),
                    fingerprint=fingerprint,
                )
            )
        return summaries

    def load_run(self, run_id: str) -> BacktestResult | None:
        """Read a persisted run. Never executes research."""
        cleaned = run_id.strip()
        if not re.fullmatch(r"[0-9a-f]{32}", cleaned):
            return None
        try:
            with self._session(read_only=True) as conn:
                return PortfolioRepository(conn).get_run(cleaned)
        except (DemoNotPreparedError, duckdb.Error, ValueError):
            # ValueError covers pydantic.ValidationError and JSON decoding of a damaged row:
            # an unreadable saved run is reported as unavailable, never half-displayed.
            return None

    # --------------------------------------------------------------- exports
    @staticmethod
    def report_html(result: BacktestResult) -> str:
        return label_synthetic_report(render_research_report(result))

    @staticmethod
    def request_json(result: BacktestResult) -> str:
        return render_request_export(result)

    @staticmethod
    def result_json(result: BacktestResult) -> str:
        return render_result_export(result)

    @staticmethod
    def download_name(result: BacktestResult, suffix: str) -> str:
        stem = _SAFE_NAME.sub("-", result.run_id)[:16] or "run"
        return f"equitytrace-demo-{stem}-{suffix}"


def strategy_pretty_json(strategy: ResearchStrategy) -> str:
    """Readable, deterministic strategy JSON that ``research validate`` accepts."""
    payload = strategy.model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _format_error(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "invalid"))
    message = message.removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0
