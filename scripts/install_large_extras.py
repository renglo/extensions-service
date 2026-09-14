#!/usr/bin/env python3
"""Install name[large-dependencies] from the wheelhouse into /build/output.

Used only by the ECS/--large Docker image build. Retries with PIP_ONLY_BINARY
from wheel_libs.json when the first install fails (e.g. hdbscan).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


# Core / shared pins: install from wheelhouse in the main pip step, but they do
# not declare [large-dependencies]. Only extension handler dists get that extra.
_SKIP_LARGE_PREFIXES = ("renglo-lib", "renglo-api", "renglo-ci")


def _wants_large_extra(name: str) -> bool:
    n = name.strip().lower()
    if not n:
        return False
    if n in _SKIP_LARGE_PREFIXES or n.startswith("renglo-lib"):
        return False
    # Shared renglo-* libs (gro, data, …) are deps; large extras live on lab/triage.
    if n.startswith("renglo-"):
        return False
    return True


def main() -> int:
    pkgs = [
        p.strip()
        for p in os.environ.get("HANDLERS_PACKAGES", "").split(",")
        if _wants_large_extra(p)
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
        spec = f"{name}[large-dependencies]"
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
