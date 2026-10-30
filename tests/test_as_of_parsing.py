"""Issue #26: every ``--as-of`` option resolves a string to the same UTC instant."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import equitytrace.cli as cli_module
import equitytrace.cli_market as cli_market_module
from equitytrace.cli import app
from equitytrace.config import clear_settings_cache
from equitytrace.database import initialize_database
from equitytrace.timeutil import parse_as_of

runner = CliRunner()

CASES = [
    # Date-only: 23:59:59 America/New_York (EST = UTC-5, EDT = UTC-4).
    ("2024-02-15", datetime(2024, 2, 16, 4, 59, 59, tzinfo=UTC)),
    ("2024-07-15", datetime(2024, 7, 16, 3, 59, 59, tzinfo=UTC)),
    # Naive datetime: Eastern wall time.
    ("2024-02-15T16:30:00", datetime(2024, 2, 15, 21, 30, tzinfo=UTC)),
    ("2024-07-15 16:30:00", datetime(2024, 7, 15, 20, 30, tzinfo=UTC)),
    # Explicit zone: absolute instant, never reinterpreted.
    ("2024-02-15T16:30:00Z", datetime(2024, 2, 15, 16, 30, tzinfo=UTC)),
    ("2024-02-15T16:30:00z", datetime(2024, 2, 15, 16, 30, tzinfo=UTC)),
    ("2024-02-15T16:30:00+00:00", datetime(2024, 2, 15, 16, 30, tzinfo=UTC)),
    ("2024-02-15T16:30:00-05:00", datetime(2024, 2, 15, 21, 30, tzinfo=UTC)),
    ("  2024-02-15  ", datetime(2024, 2, 16, 4, 59, 59, tzinfo=UTC)),
    # DST transition day: 2024-03-10 EDT begins, so end of day is UTC-4.
    ("2024-03-10", datetime(2024, 3, 11, 3, 59, 59, tzinfo=UTC)),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_parse_as_of_rules(text: str, expected: datetime) -> None:
    assert parse_as_of(text) == expected
    assert parse_as_of(text).tzinfo is not None


def test_date_only_includes_us_close_bar_and_late_evening_filings() -> None:
    # A bar available 16:15 ET (21:15 UTC) and a filing accepted 22:00 ET are both
    # inside "as of" that calendar date; the next day's open is not.
    cutoff = parse_as_of("2024-02-15")
    assert datetime(2024, 2, 15, 21, 15, tzinfo=UTC) <= cutoff
    assert datetime(2024, 2, 16, 3, 0, tzinfo=UTC) <= cutoff  # 22:00 ET
    assert datetime(2024, 2, 16, 5, 0, tzinfo=UTC) > cutoff  # 00:00 ET next day


@pytest.mark.parametrize("bad", ["", "not-a-date", "2024-13-40", "2024-02-30T10:00:00"])
def test_parse_as_of_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid date"):
        parse_as_of(bad)


def _commands() -> list[tuple[str, list[str]]]:
    return [
        ("statements", ["statements", "ALPHA", "--period", "FY2023", "--as-of", "{v}"]),
        ("factor", ["factor", "ALPHA", "revenue-growth", "--period", "FY2023", "--as-of", "{v}"]),
        ("factors", ["factors", "ALPHA", "--period", "FY2023", "--as-of", "{v}"]),
        (
            "rank",
            [
                "rank",
                "--tickers",
                "ALPHA",
                "--factor",
                "revenue-growth",
                "--period",
                "FY2023",
                "--as-of",
                "{v}",
            ],
        ),
        ("facts-as-of", ["facts-as-of", "ALPHA", "--date", "{v}"]),
        ("facts-as-of-alias", ["facts-as-of", "ALPHA", "--as-of", "{v}"]),
        ("market analytics", ["market", "analytics", "ALPHA", "--as-of", "{v}"]),
        (
            "market rank-metric",
            ["market", "rank-metric", "momentum_12_1", "ALPHA", "--as-of", "{v}"],
        ),
        ("market cap", ["market", "cap", "ALPHA", "--date", "2024-02-15", "--as-of", "{v}"]),
    ]


@pytest.mark.parametrize(("text", "expected"), CASES[:6])
@pytest.mark.parametrize(("name", "argv"), _commands(), ids=[c[0] for c in _commands()])
def test_every_command_resolves_same_instant(
    name: str,
    argv: list[str],
    text: str,
    expected: datetime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "asof.duckdb"
    monkeypatch.setenv("EQUITYTRACE_SEC_EMAIL", "tests@example.com")
    monkeypatch.setenv("EQUITYTRACE_DATABASE_PATH", str(path))
    clear_settings_cache()
    initialize_database(path)

    seen: list[datetime] = []

    def spy(value: str) -> datetime:
        resolved = parse_as_of(value)
        seen.append(resolved)
        return resolved

    monkeypatch.setattr(cli_module, "parse_as_of", spy)
    monkeypatch.setattr(cli_market_module, "parse_as_of", spy)
    result = runner.invoke(app, [a.replace("{v}", text) for a in argv])
    assert "Traceback" not in result.output, result.output
    assert seen == [expected], f"{name}: {result.output}"


def test_no_duplicate_as_of_parsers_remain() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "equitytrace"
    definitions: list[str] = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        definitions += [
            f"{path.name}:{m}" for m in re.findall(r"^def (_?parse_as_of)\(", text, re.M)
        ]
    assert definitions == ["timeutil.py:parse_as_of"]


def test_help_documents_rule(tmp_path: Path) -> None:
    result = runner.invoke(app, ["market", "analytics", "--help"], terminal_width=200)
    assert "US Eastern" in result.output
