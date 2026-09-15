"""Look-ahead, restatement, and future-bar protections."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from equitytrace.database import Database
from equitytrace.portfolio.models import PortfolioRunStatus
from helpers.financial_fixtures import make_fact
from helpers.portfolio_fixtures import (
    add_adjusted_bars,
    annual_roa_facts,
    explicit_request,
    run_backtest,
    seed_issuer,
)
from test_portfolio_backtest import D0, D1, D3, KNOWN, _seed_two_name_path


def test_future_factor_facts_do_not_change_decision(db: Database) -> None:
    _seed_two_name_path(db)
    request = explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1))
    first = run_backtest(db, request)
    assert first.status is PortfolioRunStatus.SUCCESS
    future = datetime(2025, 1, 1, tzinfo=UTC)
    with db.session() as conn:
        extra = annual_roa_facts(
            cik="0001000002",
            fiscal_year=2023,
            net_income=10.0,
            assets=100.0,
            prior_assets=100.0,
            available_at=KNOWN,
            accession="bbb-2023",
        ) + annual_roa_facts(
            cik="0001000002",
            fiscal_year=2024,
            net_income=999.0,
            assets=100.0,
            prior_assets=100.0,
            available_at=future,
            accession="bbb-future",
        )
        seed_issuer(conn, ticker="BBB", cik="0001000002", facts=extra)
    second = run_backtest(db, request)
    assert second.final_nav == first.final_nav
    assert [row.transitions[0].signal_period for row in first.rebalances] == [
        row.transitions[0].signal_period for row in second.rebalances
    ]


def test_restatement_after_decision_does_not_rewrite_history(db: Database) -> None:
    days = [D0, D1, date(2024, 1, 4), D3]
    original = KNOWN
    amendment = datetime(2024, 6, 1, tzinfo=UTC)
    with db.session() as conn:
        facts = [
            *annual_roa_facts(
                cik="0001000001",
                fiscal_year=2023,
                net_income=10.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=original,
                accession="aaa-orig",
            ),
            make_fact(
                cik="0001000001",
                concept="NetIncomeLoss",
                value=90.0,
                start=date(2023, 1, 1),
                end=date(2023, 12, 31),
                available_at=amendment,
                accession="aaa-restated",
                form="10-K/A",
                fiscal_year=2023,
                fiscal_period="FY",
            ),
        ]
        seed_issuer(conn, ticker="AAA", cik="0001000001", facts=facts)
        seed_issuer(
            conn,
            ticker="BBB",
            cik="0001000002",
            facts=annual_roa_facts(
                cik="0001000002",
                fiscal_year=2023,
                net_income=12.0,
                assets=100.0,
                prior_assets=100.0,
                available_at=original,
                accession="bbb-2023",
            ),
        )
        add_adjusted_bars(conn, "SPY", [(day, 100.0) for day in days])
        for ticker in ("AAA", "BBB"):
            add_adjusted_bars(conn, ticker, [(day, 100.0) for day in days])
    early = explicit_request(
        ("AAA", "BBB"),
        start=D0,
        end=date(2024, 1, 4),
        decisions=(D0,),
        top_n=1,
    )
    before = run_backtest(db, early)
    # After the amendment is knowable, AAA would rank first, but this check only
    # verifies the early run used the original ranking.
    held = [row.symbol for row in before.rebalances[0].transitions if row.target_weight_after > 0]
    assert held == ["BBB"]
    from equitytrace.factors.engine import FactorEngine
    from equitytrace.financials.models import FinancialPeriod

    with db.session() as conn:
        engine = FactorEngine(conn)
        period = FinancialPeriod(fiscal_year=2023, fiscal_period="FY", kind="annual")
        orig = engine.calculate(
            "AAA", "roa", period, as_of=datetime(2024, 1, 3, tzinfo=UTC), persist=False
        )
        restated = engine.calculate("AAA", "roa", period, as_of=amendment, persist=False)
    assert orig.value != restated.value
    assert before.final_nav is not None


def test_future_adjusted_bars_do_not_change_returns(db: Database) -> None:
    _seed_two_name_path(db)
    request = explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1))
    first = run_backtest(db, request)
    future_day = date(2024, 1, 8)
    future_at = datetime(2026, 1, 1, tzinfo=UTC)
    with db.session() as conn:
        add_adjusted_bars(
            conn,
            "AAA",
            [(future_day, 10000.0)],
            available_at={future_day: future_at},
        )
        add_adjusted_bars(
            conn,
            "SPY",
            [(future_day, 10000.0)],
            available_at={future_day: future_at},
        )
    second = run_backtest(db, request)
    assert second.final_nav == first.final_nav


def test_future_calendar_session_is_ignored(db: Database) -> None:
    _seed_two_name_path(db)
    request = explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0,))
    first = run_backtest(db, request)
    with db.session() as conn:
        add_adjusted_bars(
            conn,
            "SPY",
            [(date(2024, 2, 1), 100.0)],
            available_at={date(2024, 2, 1): datetime(2026, 1, 1, tzinfo=UTC)},
        )
    second = run_backtest(db, request)
    assert len(second.rebalances) == len(first.rebalances)
    assert second.final_nav == first.final_nav


def test_audit_rejects_future_price_trading_date() -> None:
    from equitytrace.portfolio.audit import audit_decision
    from equitytrace.portfolio.models import SignalSelection

    decision_session = date(2024, 1, 3)
    decision_at = datetime(2024, 1, 3, 21, 15, tzinfo=UTC)
    selection = SignalSelection(
        ranked=(),
        price_evidence_available_at=(datetime(2024, 1, 2, tzinfo=UTC),),
        price_evidence_dates=(date(2024, 1, 4),),
    )
    failures = audit_decision(
        decision_at=decision_at,
        decision_session_date=decision_session,
        target_effective_at=datetime(2024, 1, 4, 21, 15, tzinfo=UTC),
        selection=selection,
        mixed_periods=False,
    )
    assert failures
    assert any("trading date" in item for item in failures)


def test_leakage_detected_preserves_failed_audit(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from equitytrace.portfolio.engine import BacktestEngine
    from equitytrace.portfolio.models import LeakageAuditResult
    from equitytrace.repositories.portfolio import PortfolioRepository

    _seed_two_name_path(db)
    original = BacktestEngine._form_target

    def tainted(
        self: BacktestEngine,
        *args: object,
        **kwargs: object,
    ) -> object:
        target = original(self, *args, **kwargs)
        selection = target.selection.model_copy(
            update={
                "price_evidence_available_at": (datetime(2020, 1, 1, tzinfo=UTC),),
                "price_evidence_dates": (date(2099, 1, 1),),
            }
        )
        return target.model_copy(update={"selection": selection})

    monkeypatch.setattr(BacktestEngine, "_form_target", tainted)
    result = run_backtest(
        db,
        explicit_request(("AAA", "BBB"), start=D0, end=D3, decisions=(D0, D1), top_n=2),
    )
    assert result.status is PortfolioRunStatus.FAILED
    assert result.failure_reason == "leakage_detected"
    assert result.audit is not None
    assert result.audit.passed is False
    assert result.audit.failures
    assert any("trading date" in item for item in result.audit.failures)
    with db.session() as conn:
        loaded = PortfolioRepository(conn).get_run(result.run_id)
    assert loaded is not None
    assert loaded.status is PortfolioRunStatus.FAILED
    assert loaded.failure_reason == "leakage_detected"
    assert loaded.audit is not None
    assert loaded.audit.passed is False
    assert loaded.audit.failures == result.audit.failures
    LeakageAuditResult.model_validate(loaded.audit.model_dump())
