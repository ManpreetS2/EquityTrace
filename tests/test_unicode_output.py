"""Regression: Unicode CLI output and package-smoke subprocess decoding on legacy code pages.

Reproduced on Windows: ``equitytrace research validate`` raised ``UnicodeEncodeError`` when its
output was piped (cp1252), and ``scripts/package_smoke.py`` failed when the child wrote UTF-8
(``PYTHONIOENCODING=utf-8``) while the parent decoded with the locale code page.
The tests force the code page through ``PYTHONIOENCODING`` so they fail the same way on any OS.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from equitytrace.cli import ensure_unicode_output

ROOT = Path(__file__).resolve().parents[1]
ARROW = "→"
CHILD_CODE = f"print('range 2023-07-03 {ARROW} 2024-12-31 │ — ok')"


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["EQUITYTRACE_SEC_EMAIL"] = "tests@example.com"
    env.update(extra)
    return env


def _run_cli(args: list[str], **env: str) -> subprocess.CompletedProcess[bytes]:
    code = "from equitytrace.cli import app; app()"
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        env=_child_env(**env),
        cwd=ROOT,
        check=False,
    )


@pytest.mark.parametrize("encoding", ["cp1252", "ascii", "latin-1"])
def test_piped_cli_output_survives_legacy_code_pages(encoding: str) -> None:
    result = _run_cli(
        ["research", "validate", "--strategy", "examples/demo/strategy.json"],
        PYTHONIOENCODING=encoding,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"Traceback" not in result.stdout + result.stderr
    assert ARROW.encode("utf-8") in result.stdout  # the real character, not '?'
    assert result.stdout.decode("utf-8")  # whole stream is valid UTF-8


def test_user_errors_keep_their_nonzero_exit_status_and_message() -> None:
    result = _run_cli(
        ["research", "validate", "--strategy", "does-not-exist.json"], PYTHONIOENCODING="cp1252"
    )
    assert result.returncode == 1
    assert b"Strategy file not found" in result.stderr
    assert b"Traceback" not in result.stderr


def test_help_text_is_unchanged_and_decodable() -> None:
    result = _run_cli(["--help"], PYTHONIOENCODING="cp1252")
    assert result.returncode == 0
    text = result.stdout.decode("utf-8", "replace")
    assert "Usage" in text and "research" in text and "market" in text


class _Stream:
    def __init__(self, encoding: str, *, tty: bool = False, reconfigurable: bool = True) -> None:
        self.encoding = encoding
        self._tty = tty
        self.calls: list[dict[str, str]] = []
        if reconfigurable:
            self.reconfigure = self._reconfigure  # type: ignore[assignment]

    def isatty(self) -> bool:
        return self._tty

    def _reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


@pytest.mark.parametrize(
    ("stream", "expected_calls"),
    [
        (_Stream("cp1252"), [{"encoding": "utf-8", "errors": "replace"}]),
        (_Stream("utf-8"), []),
        (_Stream("UTF_8"), []),
        (_Stream("cp1252", tty=True), []),
        (_Stream("cp1252", reconfigurable=False), []),
    ],
)
def test_ensure_unicode_output_only_touches_redirected_legacy_streams(
    monkeypatch: pytest.MonkeyPatch, stream: _Stream, expected_calls: list[dict[str, str]]
) -> None:
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", _Stream("utf-8"))
    ensure_unicode_output()
    assert stream.calls == expected_calls


def _load_package_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "package_smoke_under_test", ROOT / "scripts" / "package_smoke.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("ambient", [None, "utf-8", "cp1252"])
def test_package_smoke_decodes_child_output_whatever_the_ambient_encoding(
    monkeypatch: pytest.MonkeyPatch, ambient: str | None
) -> None:
    smoke = _load_package_smoke()
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    if ambient is None:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    else:
        monkeypatch.setenv("PYTHONIOENCODING", ambient)
    output = smoke._run([sys.executable, "-c", CHILD_CODE])
    assert output.strip() == f"range 2023-07-03 {ARROW} 2024-12-31 │ — ok"


def test_package_smoke_still_fails_loudly_on_a_failing_command() -> None:
    smoke = _load_package_smoke()
    with pytest.raises(subprocess.CalledProcessError) as caught:
        smoke._run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])
    assert caught.value.returncode == 3
