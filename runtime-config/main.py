#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runtime_config import cmd_export_lambda_env, cmd_set_profile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="extensions-service runtime-config")
    parser.add_argument("extension")
    parser.add_argument("action", choices=["set-profile", "export-lambda-env"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "set-profile":
        return cmd_set_profile(args.extension, args.rest)
    return cmd_export_lambda_env(args.extension, args.rest)


if __name__ == "__main__":
    raise SystemExit(main())

