#!/usr/bin/env python3
"""Install pyproject dependencies for one handler package, skipping bundled extensions.

When building a multi-extension handlers artifact (BOM-driven), sibling extensions
are copied from source; their dist names must not be re-installed via pip or the
BOM-pinned source can be overwritten by a resolver picking a different version.

Used only inside build_lambda_package.sh (Lambda zip + ECS image). Normal
``pip install .`` in extension repos is unchanged.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# Installed separately in the Docker build before any extension deps.
DEFAULT_ALWAYS_SKIP = frozenset({"renglo-lib"})


def normalize_dist_name(name: str) -> str:
    """PEP 503 normalization for dependency / project names."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _requirement_name(spec: str) -> str:
    """Extract distribution name from a dependency specifier string."""
    spec = spec.strip()
    if not spec:
        return ""
    return normalize_dist_name(re.split(r"\s*[<>=!~;\[]", spec, maxsplit=1)[0])


def read_pyproject(package_dir: Path) -> dict:
    path = package_dir / "pyproject.toml"
    if not path.is_file():
        raise FileNotFoundError(f"pyproject.toml not found under {package_dir}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def project_dist_name(package_dir: Path) -> str:
    data = read_pyproject(package_dir)
    name = str((data.get("project") or {}).get("name") or "").strip()
    if not name:
        raise ValueError(f"project.name missing in {package_dir / 'pyproject.toml'}")
    return normalize_dist_name(name)


def project_dependencies(package_dir: Path) -> list[str]:
    data = read_pyproject(package_dir)
    raw = (data.get("project") or {}).get("dependencies") or []
    if not isinstance(raw, list):
        raise ValueError(f"project.dependencies must be a list in {package_dir}")
    return [str(item).strip() for item in raw if str(item).strip()]


def bundled_dist_names(bundled_dirs: list[Path], *, always_skip: frozenset[str]) -> set[str]:
    names: set[str] = set(always_skip)
    for package_dir in bundled_dirs:
        if not package_dir.is_dir():
            continue
        try:
            names.add(project_dist_name(package_dir))
        except (FileNotFoundError, ValueError) as exc:
            print(f"WARNING: skip bundled dir {package_dir}: {exc}", file=sys.stderr)
    return names


def deps_to_install(
    package_dir: Path,
    bundled_dirs: list[Path],
    *,
    always_skip: frozenset[str] = DEFAULT_ALWAYS_SKIP,
) -> tuple[list[str], list[str]]:
    """Return (install_specs, skipped_specs) for one package directory."""
    skip = bundled_dist_names(bundled_dirs, always_skip=always_skip)
    install: list[str] = []
    skipped: list[str] = []
    for spec in project_dependencies(package_dir):
        if _requirement_name(spec) in skip:
            skipped.append(spec)
        else:
            install.append(spec)
    return install, skipped


def install_dependencies(
    package_dir: Path,
    target: Path,
    bundled_dirs: list[Path],
    *,
    always_skip: frozenset[str] = DEFAULT_ALWAYS_SKIP,
    dry_run: bool = False,
) -> int:
    package_dir = package_dir.resolve()
    target = target.resolve()
    install, skipped = deps_to_install(package_dir, bundled_dirs, always_skip=always_skip)

    label = project_dist_name(package_dir)
    if skipped:
        print(f"==> {label}: skipping bundled deps: {', '.join(skipped)}")
    if not install:
        print(f"==> {label}: no third-party deps to pip install")
        return 0

    print(f"==> {label}: pip install {len(install)} dep(s): {', '.join(install)}")
    if dry_run:
        return 0

    target.mkdir(parents=True, exist_ok=True)
    for spec in install:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--target",
                str(target),
                spec,
            ],
            check=True,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install handler package deps, skipping dist names already bundled as source",
    )
    parser.add_argument("--package-dir", type=Path, required=True, help="Extension package/ tree")
    parser.add_argument("--target", type=Path, required=True, help="pip --target directory")
    parser.add_argument(
        "--bundled-dir",
        type=Path,
        action="append",
        default=[],
        help="Other package/ dirs in this artifact (repeatable)",
    )
    parser.add_argument(
        "--always-skip",
        action="append",
        default=[],
        help="Dist names to never pip-install (default: renglo-lib)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    args = parser.parse_args(argv)

    always_skip = DEFAULT_ALWAYS_SKIP | {normalize_dist_name(n) for n in args.always_skip}
    bundled = [args.package_dir.resolve(), *[p.resolve() for p in args.bundled_dir]]
    # Deduplicate while preserving order
    seen: set[Path] = set()
    unique_bundled: list[Path] = []
    for path in bundled:
        if path not in seen:
            seen.add(path)
            unique_bundled.append(path)

    try:
        return install_dependencies(
            args.package_dir,
            args.target,
            unique_bundled,
            always_skip=always_skip,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
