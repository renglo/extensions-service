#!/usr/bin/env python3
"""Install name[large-dependencies] from the wheelhouse into /build/output.

Used only by the ECS/--large Docker image build. Retries with PIP_ONLY_BINARY
from wheel_libs.json when the first install fails (e.g. hdbscan).

Candidates are packages in HANDLERS_PACKAGES that are not core runtime pins and
whose wheelhouse artifact declares ``Provides-Extra: large-dependencies``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from handlers_core_packages import is_core_handlers_package, normalize_dist_name

LARGE_EXTRA = "large-dependencies"
WHEELHOUSE = Path("/build/wheelhouse")

_SDIST_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.+-]+)-(?P<version>[0-9][^/]*)\.(?:tar\.gz|zip)$",
    re.IGNORECASE,
)


def _artifact_matches(path: Path, want_norm: str) -> bool:
    if path.name.endswith((".tar.gz", ".zip")):
        m = _SDIST_RE.match(path.name)
        return bool(m and normalize_dist_name(m.group("name")) == want_norm)
    if path.suffix == ".whl":
        stem = path.name[: -len(".whl")]
        parts = stem.split("-")
        return len(parts) >= 2 and normalize_dist_name(parts[0]) == want_norm
    return False


def _find_artifacts(wheels_dir: Path, dist_name: str) -> list[Path]:
    if not wheels_dir.is_dir():
        return []
    want = normalize_dist_name(dist_name)
    return [p for p in sorted(wheels_dir.iterdir()) if p.is_file() and _artifact_matches(p, want)]


def _provides_extra_from_metadata_text(text: str, extra: str) -> bool:
    want = extra.strip().lower()
    for line in text.splitlines():
        if line.lower().startswith("provides-extra:"):
            value = line.split(":", 1)[1].strip().lower()
            if value == want:
                return True
    return False


def _provides_extra_from_pyproject(text: str, extra: str) -> bool:
    try:
        data = tomllib.loads(text)
    except Exception:
        return False
    opts = (data.get("project") or {}).get("optional-dependencies") or {}
    return extra in opts


def _wheel_provides_extra(path: Path, extra: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
            if not names:
                return False
            return _provides_extra_from_metadata_text(
                zf.read(names[0]).decode("utf-8", errors="replace"), extra
            )
    except (OSError, zipfile.BadZipFile):
        return False


def _sdist_provides_extra(path: Path, extra: str) -> bool:
    try:
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as tf:
                members = [m for m in tf.getmembers() if m.isfile()]
                for m in members:
                    base = Path(m.name).name
                    if base == "PKG-INFO":
                        f = tf.extractfile(m)
                        if f and _provides_extra_from_metadata_text(
                            f.read().decode("utf-8", errors="replace"), extra
                        ):
                            return True
                for m in members:
                    if Path(m.name).name == "pyproject.toml":
                        f = tf.extractfile(m)
                        if f and _provides_extra_from_pyproject(
                            f.read().decode("utf-8", errors="replace"), extra
                        ):
                            return True
        elif path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if Path(name).name == "PKG-INFO":
                        if _provides_extra_from_metadata_text(
                            zf.read(name).decode("utf-8", errors="replace"), extra
                        ):
                            return True
                for name in zf.namelist():
                    if Path(name).name == "pyproject.toml":
                        if _provides_extra_from_pyproject(
                            zf.read(name).decode("utf-8", errors="replace"), extra
                        ):
                            return True
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return False
    return False


def dist_provides_extra(
    wheelhouse: Path, dist_name: str, extra: str = LARGE_EXTRA
) -> bool:
    """True if a wheelhouse artifact for dist declares Provides-Extra / optional-dep."""
    arts = _find_artifacts(wheelhouse, dist_name)
    if not arts:
        return False
    wheels = [p for p in arts if p.suffix == ".whl"]
    sdists = [p for p in arts if p.name.endswith((".tar.gz", ".zip"))]
    for path in wheels + sdists:
        if path.suffix == ".whl":
            if _wheel_provides_extra(path, extra):
                return True
        elif _sdist_provides_extra(path, extra):
            return True
    return False


def _wants_large_extra(name: str, wheelhouse: Path) -> bool:
    n = name.strip()
    if not n or is_core_handlers_package(n):
        return False
    return dist_provides_extra(wheelhouse, n, LARGE_EXTRA)


def main() -> int:
    pkgs = [
        p.strip()
        for p in os.environ.get("HANDLERS_PACKAGES", "").split(",")
        if _wants_large_extra(p, WHEELHOUSE)
    ]
    if not pkgs:
        print(
            "WARNING: no handler packages eligible for [large-dependencies]; skipping",
            file=sys.stderr,
        )
        return 0

    only_binary: list[str] = []
    libs_path = "/build/wheel_libs.json"
    try:
        raw = json.loads(open(libs_path, encoding="utf-8").read())
        if isinstance(raw, list):
            only_binary = [str(x) for x in raw]
    except (OSError, json.JSONDecodeError):
        only_binary = []

    base = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links=/build/wheelhouse",
        "--target",
        "/build/output",
    ]
    for name in pkgs:
        spec = f"{name}[{LARGE_EXTRA}]"
        print(f"install large extra: {spec}")
        rc = subprocess.run(base + [spec], check=False).returncode
        if rc != 0 and only_binary:
            env = os.environ.copy()
            env["PIP_ONLY_BINARY"] = ",".join(only_binary)
            print(f"retry {spec} with PIP_ONLY_BINARY={env['PIP_ONLY_BINARY']}")
            rc = subprocess.run(base + [spec], env=env, check=False).returncode
        if rc != 0:
            print(f"ERROR: failed to install {spec}", file=sys.stderr)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
