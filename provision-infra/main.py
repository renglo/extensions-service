#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from provision_infra import cmd_apply, cmd_destroy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="extensions-service provision-infra")
    parser.add_argument("extension")
    parser.add_argument("action", choices=["apply", "destroy"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "apply":
        return cmd_apply(args.extension, args.rest)
    return cmd_destroy(args.extension, args.rest)


if __name__ == "__main__":
    raise SystemExit(main())

