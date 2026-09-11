#!/usr/bin/env python3
"""Merge handlers_config.json extras into /build/output (Docker build helper)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    base = Path("/build/output/handlers_config.json")
    extras = Path("/build/handlers-assets/extras")
    data = json.loads(base.read_text(encoding="utf-8"))
    handlers = dict(data.get("handlers") or {})
    if extras.is_dir():
        for cfg in sorted(extras.glob("*/handlers_config.json")):
            extra = json.loads(cfg.read_text(encoding="utf-8"))
            handlers.update(extra.get("handlers") or {})
            print(f"Merged handlers_config.json from {cfg.parent.name}")
    base.write_text(json.dumps({"handlers": handlers}, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
