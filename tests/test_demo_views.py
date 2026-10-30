"""Pure presentation helpers: chart specs, time labels and honest metric cells."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from equitytrace.demo.service import DemoService
from equitytrace.demo.views import (
    NAV_AXIS_NOTE,
    allocation_chart,
    allocation_rows,
    decision_detail,
    drawdown_chart,
    equity_rows,
    et_text,
    metric_cells,
    nav_chart,
    rebalance_summary_rows,
)
from equitytrace.portfolio.models import BacktestResult


@pytest.fixture(scope="module")
def result(demo_prototype: Path) -> BacktestResult:
    service = DemoService(demo_prototype.parent / "views-copy")
    import shutil

    shutil.copytree(demo_prototype, service.paths.directory)
    outcome = service.validate_strategy(service.presets()[0].payload)
    assert outcome.strategy is not None
    return service.run_strategy(outcome.strategy, token="views")


def test_eastern_labels_follow_daylight_saving_time() -> None:
    assert et_text(datetime(2023, 7, 31, 20, 15, tzinfo=UTC)) == "2023-07-31 16:15 EDT"
    assert et_text(datetime(2024, 1, 31, 21, 15, tzinfo=UTC)) == "2024-01-31 16:15 EST"


def test_nav_chart_uses_a_data_scaled_axis_with_a_visible_starting_reference(
    result: BacktestResult,
) -> None:
    rows = equity_rows(result)
    data, spec = nav_chart(rows, result.request.initial_nav)
    json.dumps({"data": data, "spec": spec})  # plain JSON, no date objects
    line, rule = spec["layer"]
    assert line["encoding"]["y"]["scale"] == {"zero": False}
    assert rule["encoding"]["y"] == {"datum": result.request.initial_nav}
    assert {row["series"] for row in data} == {"Portfolio NAV", "Benchmark NAV"}
    assert len(data) == 2 * len(result.equity)
    assert "does not start at zero" in NAV_AXIS_NOTE and "dashed" in NAV_AXIS_NOTE
    # The first point of both series is the starting NAV: the lines share one origin.
    assert {row["nav"] for row in data[:2]} == {result.request.initial_nav}


def test_missing_benchmark_values_are_omitted_never_drawn_as_zero(result: BacktestResult) -> None:
    rows = [dict(row) for row in equity_rows(result)]
    for row in rows[10:20]:
        row["Benchmark NAV"] = None
    data, _spec = nav_chart(rows, 1.0)
    benchmark = [row for row in data if row["series"] == "Benchmark NAV"]
    assert len(benchmark) == len(rows) - 10
    assert all(row["nav"] is not None and row["nav"] > 0 for row in data)


def test_drawdown_chart_is_a_percentage_below_the_running_peak(result: BacktestResult) -> None:
    data, spec = drawdown_chart(equity_rows(result))
    json.dumps({"data": data, "spec": spec})
    assert all(point["drawdown"] <= 0 for point in data)
    assert min(point["drawdown"] for point in data) == pytest.approx(
        min(p.drawdown for p in result.equity)
    )
    assert spec["encoding"]["y"]["axis"]["format"] == ".0%"


def test_allocation_chart_stacks_to_one_hundred_percent_per_rebalance(
    result: BacktestResult,
) -> None:
    rows = allocation_rows(result)
    data, spec = allocation_chart(rows)
    json.dumps({"data": data, "spec": spec})
    assert all(row["target_weight"] > 0 for row in data)  # no zero bars for unheld names
    by_session: dict[str, float] = {}
    for row in data:
        by_session[row["effective"]] = by_session.get(row["effective"], 0.0) + row["target_weight"]
        date.fromisoformat(row["effective"])
    assert len(by_session) == result.metrics.successful_rebalance_count  # type: ignore[union-attr]
    assert all(total == pytest.approx(1.0) for total in by_session.values())
    assert spec["encoding"]["y"]["scale"] == {"domain": [0, 1]}


def test_rebalance_table_shows_exchange_time_and_plain_strings(result: BacktestResult) -> None:
    rows = rebalance_summary_rows(result)
    assert rows[0]["Decision (ET)"] == "2023-07-31 16:15 EDT"
    assert rows[0]["Target effective (ET)"] == "2023-08-01 16:15 EDT"
    assert any("EST" in str(row["Decision (ET)"]) for row in rows)  # crosses the DST change
    assert all(isinstance(row["Decision (ET)"], str) for row in rows)


def test_decision_detail_exposes_raw_provenance_for_inspection(result: BacktestResult) -> None:
    detail = decision_detail(result.rebalances[0])
    held = {row["Symbol"] for row in detail.rows if row["Target weight after"] > 0}
    assert held <= set(detail.provenance)
    sample: dict[str, Any] = detail.provenance[sorted(held)[0]]
    assert sample["factor"] == "roa" and sample["ranking_direction"] == "higher_is_better"
    assert sample["source_filings"] and sample["inputs"]


def test_metric_cells_never_turn_missing_values_into_zero(result: BacktestResult) -> None:
    lacking = result.model_copy(
        update={
            "metrics": result.metrics.model_copy(  # type: ignore[union-attr]
                update={"sharpe_ratio": None, "annualized_return": None, "interval_count": 1}
            )
        }
    )
    cells = {cell.key: cell for cell in metric_cells(lacking)}
    for key in ("sharpe_ratio", "annualized_return"):
        assert cells[key].value is None and cells[key].unavailable_reason
    assert cells["final_nav"].value == result.final_nav
