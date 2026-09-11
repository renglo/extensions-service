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


def main() -> int:
    pkgs = [p.strip() for p in os.environ.get("HANDLERS_PACKAGES", "").split(",") if p.strip()]
    if not pkgs:
        print("ERROR: HANDLERS_PACKAGES is empty", file=sys.stderr)
        return 1

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
