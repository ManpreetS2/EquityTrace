"""Issue #23: ties at the top-N cutoff are broken alphabetically and must be visible."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, date, datetime

import pytest

from equitytrace.database import Database
from equitytrace.portfolio.models import TOP_N_TIE_WARNING, PortfolioRunStatus
from equitytrace.reporting.export import render_result_export
from equitytrace.reporting.html import render_research_report
from equitytrace.repositories.portfolio import PortfolioRepository
from helpers.portfolio_fixtures import (
    add_adjusted_bars,
    annual_roa_facts,
    explicit_request,
    run_backtest,
    seed_issuer,
)

D0, D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)
KNOWN = datetime(2023, 6, 1, tzinfo=UTC)
DAYS = [D0, D1, D2, D3]


def _seed(db: Database, incomes: dict[str, float]) -> None:
    with db.session() as conn:
        for index, (ticker, income) in enumerate(incomes.items(), start=1):
            cik = f"000100000{index}"
            seed_issuer(
                conn,
                ticker=ticker,
                cik=cik,
                facts=annual_roa_facts(
                    cik=cik,
                    fiscal_year=2023,
                    net_income=income,
                    assets=100.0,
                    prior_assets=100.0,
                    available_at=KNOWN,
                    accession=f"{ticker}-2023",
                ),
            )
            add_adjusted_bars(conn, ticker, [(day, 100.0) for day in DAYS])
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in DAYS])


def _run(db: Database, tickers: tuple[str, ...], top_n: int):  # type: ignore[no-untyped-def]
    return run_backtest(
        db, explicit_request(tickers, start=D0, end=D3, decisions=(D0,), top_n=top_n)
    )


def _held(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [t.symbol for t in result.rebalances[0].transitions if t.target_weight_after > 0]


def _tie_detail(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    details = []
    for transition in result.rebalances[0].transitions:
        if transition.signal_provenance_json:
            payload = json.loads(transition.signal_provenance_json)
            if TOP_N_TIE_WARNING in payload:
                details.append(payload[TOP_N_TIE_WARNING])
    assert details, "no tie detail persisted on any selected transition"
    assert all(item == details[0] for item in details)
    return details[0]


def test_tie_at_cutoff_is_alphabetical_and_visible(db: Database) -> None:
    _seed(db, {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0, "DDD": 5.0})
    result = _run(db, ("CCC", "DDD", "BBB", "AAA"), top_n=2)
    assert result.status is PortfolioRunStatus.SUCCESS
    assert _held(result) == ["AAA", "BBB"]  # backward-compatible selection
    assert TOP_N_TIE_WARNING in result.warnings
    assert result.audit is not None and TOP_N_TIE_WARNING in result.audit.warnings
    detail = _tie_detail(result)
    assert detail["rule"] == "ticker_ascending"
    assert detail["tied_tickers"] == ["AAA", "BBB", "CCC"]
    assert detail["selected_tickers"] == ["AAA", "BBB"]
    assert detail["excluded_tickers"] == ["CCC"]


def test_tie_info_survives_persistence_export_and_html(db: Database) -> None:
    _seed(db, {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0})
    result = _run(db, ("AAA", "BBB", "CCC"), top_n=2)
    with db.session() as conn:
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert TOP_N_TIE_WARNING in loaded.warnings
    assert _tie_detail(loaded)["excluded_tickers"] == ["CCC"]

    exported = json.loads(render_result_export(loaded))["result"]
    assert TOP_N_TIE_WARNING in exported["warnings"]
    assert TOP_N_TIE_WARNING in exported["audit"]["warnings"]
    provenance = [
        json.loads(t["signal_provenance_json"])
        for t in exported["rebalances"][0]["transitions"]
        if t["signal_provenance_json"]
    ]
    assert provenance[0][TOP_N_TIE_WARNING]["rule"] == "ticker_ascending"

    html = render_research_report(loaded)
    assert "Top-N tie at cutoff" in html
    assert "ticker_ascending" in html
    assert "AAA, BBB, CCC" in html  # tied group
    assert "chosen by ticker spelling" in html


@pytest.mark.parametrize(
    ("incomes", "top_n"),
    [
        ({"AAA": 20.0, "BBB": 10.0, "CCC": 5.0}, 2),  # no ties at all
        ({"AAA": 10.0, "BBB": 10.0, "CCC": 10.0}, 3),  # tie wholly inside the selection
        ({"AAA": 20.0, "BBB": 10.0, "CCC": 10.0}, 1),  # tie wholly below the cutoff
        ({"AAA": 10.0, "BBB": 10.0, "CCC": 5.0}, 2),  # tie ends exactly at the cutoff
    ],
)
def test_clean_cutoff_has_no_tie_warning(
    db: Database, incomes: dict[str, float], top_n: int
) -> None:
    _seed(db, incomes)
    result = _run(db, tuple(incomes), top_n=top_n)
    assert result.status is PortfolioRunStatus.SUCCESS
    assert TOP_N_TIE_WARNING not in result.warnings
    assert result.audit is not None and TOP_N_TIE_WARNING not in result.audit.warnings
    html = render_research_report(result)
    assert "Top-N tie at cutoff" not in html
    for transition in result.rebalances[0].transitions:
        assert TOP_N_TIE_WARNING not in (transition.signal_provenance_json or "")


def test_selection_and_tie_detail_independent_of_universe_order_and_hash_seed(
    db: Database,
) -> None:
    """Listed in the CI determinism matrix (PYTHONHASHSEED 0/1/42)."""
    _seed(db, {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0, "DDD": 10.0})
    baseline = None
    for order in itertools.permutations(("AAA", "BBB", "CCC", "DDD")):
        result = run_backtest(
            db,
            explicit_request(order, start=D0, end=D3, decisions=(D0,), top_n=2),
            persist=False,
        )
        snapshot = (_held(result), json.dumps(_tie_detail(result), sort_keys=True))
        baseline = baseline or snapshot
        assert snapshot == baseline
    assert baseline is not None and baseline[0] == ["AAA", "BBB"]
