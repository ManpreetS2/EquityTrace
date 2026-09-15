"""Build wheel/sdist and verify a clean install, not the repo source tree."""

from __future__ import annotations

import email
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=merged,
        check=True,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "") + (result.stderr or "")


def _project_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = data["project"]["version"]
    if not isinstance(version, str) or not version:
        raise SystemExit("pyproject.toml is missing project.version")
    return version


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lower()


def _assert_clean_names(names: list[str], *, label: str) -> None:
    for raw in names:
        lowered = _normalize(raw)
        base = lowered.rsplit("/", 1)[-1]
        if base == ".env" or base.startswith(".env."):
            raise SystemExit(f"{label} contains env file: {raw}")
        if base.endswith(".duckdb") or base.endswith(".wal"):
            raise SystemExit(f"{label} contains database file: {raw}")
        packed = f"/{lowered}/"
        for fragment in (
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "/.venv/",
            "/venv/",
            "/__pycache__/",
            "/users/",
            "c:/users/",
        ):
            if fragment in packed or fragment in lowered:
                raise SystemExit(f"{label} contains forbidden path {raw!r}")


def _metadata_from_wheel(wheel: Path) -> email.message.Message:
    with zipfile.ZipFile(wheel) as archive:
        matches = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(matches) != 1:
            raise SystemExit(f"expected one METADATA file in wheel, found {matches}")
        return email.message_from_bytes(archive.read(matches[0]))


def _has_license_file(names: list[str]) -> bool:
    return any(_normalize(name).rsplit("/", 1)[-1] in {"license", "license.txt"} for name in names)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    version = _project_version(root)
    dist = root / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    build = root / "build"
    if build.exists():
        shutil.rmtree(build)

    print(f"Building equitytrace {version}", flush=True)
    _run(["uv", "build"], cwd=root)

    wheel = dist / f"equitytrace-{version}-py3-none-any.whl"
    sdist = dist / f"equitytrace-{version}.tar.gz"
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if wheels != [wheel]:
        raise SystemExit(f"expected exactly {wheel.name}, found {[p.name for p in wheels]}")
    if sdists != [sdist]:
        raise SystemExit(f"expected exactly {sdist.name}, found {[p.name for p in sdists]}")
    if ".dev" in version:
        print(f"Development version {version} is allowed on this branch.", flush=True)

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        entry = archive.read(
            next(name for name in wheel_names if name.endswith(".dist-info/entry_points.txt"))
        ).decode("utf-8")
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = [member.name for member in archive.getmembers() if member.isfile()]
    _assert_clean_names(wheel_names, label="wheel")
    _assert_clean_names(sdist_names, label="sdist")

    meta = _metadata_from_wheel(wheel)
    if meta.get("Name") != "equitytrace":
        raise SystemExit(f"wheel Name={meta.get('Name')!r}")
    if meta.get("Version") != version:
        raise SystemExit(f"wheel Version={meta.get('Version')!r}, expected {version}")
    requires = meta.get("Requires-Python", "")
    if "3.12" not in requires:
        raise SystemExit(f"Requires-Python missing 3.12: {requires!r}")
    license_meta = (meta.get("License-Expression") or meta.get("License") or "").strip()
    if "MIT" not in license_meta:
        raise SystemExit(f"license metadata missing MIT: {license_meta!r}")
    if "equitytrace=equitytrace.cli:app" not in entry.replace(" ", "").lower():
        raise SystemExit(f"missing console entry point:\n{entry}")
    if not _has_license_file(wheel_names):
        raise SystemExit("wheel is missing the LICENSE file")
    if not _has_license_file(sdist_names):
        raise SystemExit("sdist is missing the LICENSE file")

    import_probe = f"""
from pathlib import Path
import equitytrace
from importlib.metadata import version
path = Path(equitytrace.__file__).resolve()
print("VERSION", equitytrace.__version__)
print("META", version("equitytrace"))
print("FILE", path)
repo = Path({str(root)!r}).resolve()
if repo in path.parents and (repo / "src") in path.parents:
    raise SystemExit(f"imported repo source: {{path}}")
if equitytrace.__version__ != {version!r} or version("equitytrace") != {version!r}:
    raise SystemExit("installed version does not match pyproject")
"""
    with tempfile.TemporaryDirectory(prefix="et-pkg-smoke-") as tmp:
        # Isolated envs live under the temp dir, not the checkout.
        env = {"UV_CACHE_DIR": str(Path(tmp) / "uv-cache")}
        wheel_out = _run(
            [
                "uv",
                "run",
                "--isolated",
                "--no-project",
                "--with",
                str(wheel),
                "python",
                "-c",
                import_probe,
            ],
            env=env,
        )
        sdist_out = _run(
            [
                "uv",
                "run",
                "--isolated",
                "--no-project",
                "--with",
                str(sdist),
                "python",
                "-c",
                import_probe,
            ],
            env=env,
        )
        print(wheel_out, flush=True)
        print(sdist_out, flush=True)
        for artifact, extra in ((wheel, ("market", "--help")), (sdist, ())):
            version_out = _run(
                [
                    "uv",
                    "run",
                    "--isolated",
                    "--no-project",
                    "--with",
                    str(artifact),
                    "equitytrace",
                    "version",
                ],
                env=env,
            )
            help_out = _run(
                [
                    "uv",
                    "run",
                    "--isolated",
                    "--no-project",
                    "--with",
                    str(artifact),
                    "equitytrace",
                    "--help",
                ],
                env=env,
            )
            if version not in version_out:
                raise SystemExit(f"CLI version mismatch for {artifact.name}: {version_out!r}")
            if "Usage" not in help_out and "usage" not in help_out.lower():
                raise SystemExit(f"CLI help failed for {artifact.name}")
            if extra:
                extra_out = _run(
                    [
                        "uv",
                        "run",
                        "--isolated",
                        "--no-project",
                        "--with",
                        str(artifact),
                        "equitytrace",
                        *extra,
                    ],
                    env=env,
                )
                if "analytics" not in extra_out.lower():
                    raise SystemExit(f"market help missing analytics:\n{extra_out}")

    print("package-smoke ok", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stdout or "")
        sys.stderr.write(exc.stderr or "")
        raise SystemExit(exc.returncode) from exc
