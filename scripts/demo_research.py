#!/usr/bin/env python3
"""Offline EquityTrace research demonstration (synthetic evidence only).

This script seeds a temporary DuckDB with clearly labeled synthetic issuers,
prices, and fiscal-year facts, then runs the real BacktestEngine / report /
export path. It does not call the SEC or Twelve Data and requires no API keys.

Generated NAV paths are synthetic research illustrations, not historical
investment performance.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from equitytrace.database import initialize_database
from equitytrace.demo.service import SYNTHETIC_REPORT_BANNER, label_synthetic_report
from equitytrace.market.availability import bar_available_at
from equitytrace.market.models import DailyPriceBar, MarketDataProviderName, PriceAdjustmentMode
from equitytrace.models import FinancialFact, Issuer, Security
from equitytrace.portfolio.engine import BacktestEngine
from equitytrace.portfolio.models import PortfolioRunStatus
from equitytrace.reporting.export import export_run_json, write_html_report
from equitytrace.reporting.html import render_research_report
from equitytrace.repositories.ingestion import save_company_snapshot
from equitytrace.repositories.market import MarketRepository
from equitytrace.repositories.portfolio import PortfolioRepository
from equitytrace.strategy.io import load_strategy
from equitytrace.strategy.models import fingerprint_request, fingerprint_strategy

# Synthetic calendar sessions used by the demo strategy.
D0 = date(2024, 1, 2)
D1 = date(2024, 1, 3)
D2 = date(2024, 1, 4)
D3 = date(2024, 1, 5)
# Fiscal years end 31 December. The FY2022 10-K is public only after its (synthetic)
# acceptance in March 2023, and the FY2021 comparative 10-K a year earlier. The research
# window is January 2024, when FY2022 is the newest annual report anyone could have seen.
KNOWN = datetime(2023, 3, 1, 21, 30, tzinfo=UTC)
PRIOR_KNOWN = datetime(2022, 3, 1, 21, 30, tzinfo=UTC)
FETCHED = datetime(2025, 1, 2, tzinfo=UTC)

BANNER = SYNTHETIC_REPORT_BANNER


def _label_report(html: str) -> str:
    """Stamp the standalone demo report so it cannot be mistaken for real results."""
    return label_synthetic_report(html)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _make_fact(
    *,
    cik: str,
    concept: str,
    value: float,
    start: date | None,
    end: date,
    available_at: datetime,
    accession: str,
    fiscal_year: int,
    fiscal_period: str = "FY",
    unit: str = "USD",
) -> FinancialFact:
    return FinancialFact(
        cik=cik,
        taxonomy="us-gaap",
        concept=concept,
        unit=unit,
        value=value,
        start_date=start,
        end_date=end,
        filing_date=available_at.date(),
        acceptance_datetime=available_at,
        available_at=available_at,
        accession_number=accession,
        form="10-K",
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        frame=None,
    ).with_fact_id()


def _annual_roa_facts(
    *,
    cik: str,
    fiscal_year: int,
    net_income: float,
    assets: float,
    prior_assets: float,
    available_at: datetime,
    prior_available_at: datetime,
    accession: str,
) -> list[FinancialFact]:
    start = date(fiscal_year, 1, 1)
    end = date(fiscal_year, 12, 31)
    prior_end = date(fiscal_year - 1, 12, 31)
    prior_start = date(fiscal_year - 1, 1, 1)
    return [
        _make_fact(
            cik=cik,
            concept="NetIncomeLoss",
            value=net_income,
            start=start,
            end=end,
            available_at=available_at,
            accession=accession,
            fiscal_year=fiscal_year,
        ),
        _make_fact(
            cik=cik,
            concept="Assets",
            value=assets,
            start=None,
            end=end,
            available_at=available_at,
            accession=accession,
            fiscal_year=fiscal_year,
        ),
        _make_fact(
            cik=cik,
            concept="Assets",
            value=prior_assets,
            start=None,
            end=prior_end,
            available_at=prior_available_at,
            accession=accession + "-P",
            fiscal_year=fiscal_year - 1,
        ),
        _make_fact(
            cik=cik,
            concept="NetIncomeLoss",
            value=prior_assets / 10.0,
            start=prior_start,
            end=prior_end,
            available_at=prior_available_at,
            accession=accession + "-P",
            fiscal_year=fiscal_year - 1,
        ),
    ]


def _add_adjusted_bars(
    conn: object,
    ticker: str,
    points: list[tuple[date, float]],
) -> None:
    repo = MarketRepository(conn)  # type: ignore[arg-type]
    instrument = repo.get_or_create_instrument(
        ticker,
        security_ticker=ticker,
        issuer_cik=None,
    )
    bars: list[DailyPriceBar] = []
    for day, close in points:
        price = Decimal(str(close))
        bars.append(
            DailyPriceBar(
                instrument_id=instrument.instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                trading_date=day,
                adjustment_mode=PriceAdjustmentMode.ALL,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=100,
                available_at=bar_available_at(day, "America/New_York"),
                fetched_at=FETCHED,
            )
        )
    if bars:
        repo.upsert_price_bars(bars)


def seed_synthetic_universe(conn: object) -> None:
    """Seed two synthetic issuers + SPY calendar/benchmark bars."""
    days = [D0, D1, D2, D3]
    save_company_snapshot(
        conn,  # type: ignore[arg-type]
        issuer=Issuer(cik="0001000001", legal_name="SYNTHETIC AAA Corp"),
        securities=[Security(ticker="AAA", cik="0001000001", is_primary=True)],
        filings=[],
        facts=_annual_roa_facts(
            cik="0001000001",
            fiscal_year=2022,
            net_income=20.0,
            assets=100.0,
            prior_assets=100.0,
            available_at=KNOWN,
            prior_available_at=PRIOR_KNOWN,
            accession="demo-aaa-2022",
        ),
    )
    save_company_snapshot(
        conn,  # type: ignore[arg-type]
        issuer=Issuer(cik="0001000002", legal_name="SYNTHETIC BBB Corp"),
        securities=[Security(ticker="BBB", cik="0001000002", is_primary=True)],
        filings=[],
        facts=_annual_roa_facts(
            cik="0001000002",
            fiscal_year=2022,
            net_income=10.0,
            assets=100.0,
            prior_assets=100.0,
            available_at=KNOWN,
            prior_available_at=PRIOR_KNOWN,
            accession="demo-bbb-2022",
        ),
    )
    _add_adjusted_bars(conn, "SPY", [(day, 100.0 + index) for index, day in enumerate(days)])
    _add_adjusted_bars(
        conn,
        "AAA",
        [(D0, 100.0), (D1, 100.0), (D2, 110.0), (D3, 104.5)],
    )
    _add_adjusted_bars(
        conn,
        "BBB",
        [(D0, 100.0), (D1, 100.0), (D2, 100.0), (D3, 100.0)],
    )


def default_strategy_path() -> Path:
    return _repo_root() / "examples" / "demo" / "strategy.json"


def write_default_strategy(path: Path) -> Path:
    payload = {
        "name": "Synthetic ROA equal-weight demo",
        "description": (
            "SYNTHETIC DEMO ONLY. Uses fictional AAA/BBB evidence. "
            "Not investment advice and not historical performance."
        ),
        "tickers": ["BBB", "AAA"],
        "factor": "roa",
        "top_n": 2,
        "baseline": "equal_weight",
        "start_date": D0.isoformat(),
        "end_date": D3.isoformat(),
        "schedule": "explicit",
        "explicit_dates": [D1.isoformat(), D0.isoformat()],
        "calendar_symbol": "SPY",
        "benchmark_symbol": "SPY",
        "cost_bps": 10.0,
        "initial_nav": 1.0,
        "provider": "twelve_data",
        "adjustment_mode": "all",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def run_demo(*, output_dir: Path, keep_db: bool = False) -> dict[str, object]:
    """Run one full offline research demonstration. Returns summary metadata."""
    output_dir.mkdir(parents=True, exist_ok=True)
    strategy_path = default_strategy_path()
    if not strategy_path.exists():
        write_default_strategy(strategy_path)

    strategy = load_strategy(strategy_path)
    request = strategy.to_backtest_request()
    digest = fingerprint_strategy(strategy)

    work = Path(tempfile.mkdtemp(prefix="equitytrace-demo-"))
    db_path = work / "demo.duckdb"
    try:
        db = initialize_database(db_path)
        with db.session() as conn:
            seed_synthetic_universe(conn)
            result = BacktestEngine(conn).run(request)
            loaded = PortfolioRepository(conn).get_run(result.run_id)
        if loaded is None:
            raise RuntimeError("Demo failed to persist/load the research run.")
        if loaded.status is not PortfolioRunStatus.SUCCESS:
            raise RuntimeError(
                f"Demo research did not succeed: status={loaded.status.value} "
                f"reason={loaded.failure_reason!r}"
            )
        if fingerprint_request(loaded.request) != digest:
            raise RuntimeError("Persisted fingerprint does not match strategy fingerprint.")

        report_path = output_dir / "research_report.html"
        export_dir = output_dir / "export"
        html = _label_report(render_research_report(loaded))
        (output_dir / "SYNTHETIC_DEMO.txt").write_text(BANNER + "\n", encoding="utf-8")
        write_html_report(report_path, html, force=True)
        request_json, result_json = export_run_json(loaded, export_dir, force=True)

        # Byte-identical regeneration check for the same persisted run.
        if _label_report(render_research_report(loaded)) != html:
            raise RuntimeError("HTML report is not deterministic for the persisted run.")
        export_b = output_dir / "export_repeat"
        req_b, res_b = export_run_json(loaded, export_b, force=True)
        if req_b.read_bytes() != request_json.read_bytes():
            raise RuntimeError("request.json is not byte-identical across exports.")
        if res_b.read_bytes() != result_json.read_bytes():
            raise RuntimeError("result.json is not byte-identical across exports.")
        if (
            write_html_report(
                output_dir / "research_report_repeat.html",
                _label_report(render_research_report(loaded)),
                force=True,
            ).read_bytes()
            != report_path.read_bytes()
        ):
            raise RuntimeError("HTML report is not byte-identical across writes.")

        metrics = loaded.metrics
        summary: dict[str, object] = {
            "banner": BANNER,
            "status": loaded.status.value,
            "run_id": loaded.run_id,
            "fingerprint": digest,
            "package_version": loaded.package_version,
            "successful_rebalances": (
                metrics.successful_rebalance_count if metrics is not None else 0
            ),
            "equity_points": len(loaded.equity),
            "transition_rows": sum(len(row.transitions) for row in loaded.rebalances),
            "signal_rows": sum(
                1
                for row in loaded.rebalances
                for item in row.transitions
                if item.signal_value is not None or item.signal_rank is not None
            ),
            "warnings": list(loaded.warnings),
            "report_path": str(report_path.resolve()),
            "request_json": str(request_json.resolve()),
            "result_json": str(result_json.resolve()),
            "final_nav": loaded.final_nav,
        }
        return summary
    finally:
        if keep_db:
            retained = output_dir / "demo.duckdb"
            shutil.copy2(db_path, retained)
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the offline EquityTrace synthetic research demonstration."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_repo_root() / "examples" / "demo" / "out",
        help="Directory for the HTML report and JSON exports (gitignored).",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Also copy the temporary DuckDB into the output directory.",
    )
    args = parser.parse_args(argv)
    print(BANNER, flush=True)
    try:
        summary = run_demo(output_dir=args.output_dir, keep_db=args.keep_db)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    print(f"\nOpen the HTML report:\n  {summary['report_path']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
