#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploy_flow import cmd_build, cmd_publish, cmd_push  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="extensions-service deploy")
    parser.add_argument("extension")
    parser.add_argument("action", choices=["build", "push", "publish"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "build":
        return cmd_build(args.extension, args.rest)
    if args.action == "push":
        return cmd_push(args.extension, args.rest)
    return cmd_publish(args.extension, args.rest)


if __name__ == "__main__":
    raise SystemExit(main())

