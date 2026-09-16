"""Shared runtime pins for handlers images (not ECS large-extra owners).

These dists install from the wheelhouse in the main ``pip install --packages``
step. They are never candidates for ``[large-dependencies]``; that extra is
detected from package metadata (``Provides-Extra``) on extension dists.
"""

from __future__ import annotations

import re

# PyPI / BOM dist names (PEP 503 normalized comparison via normalize_dist_name).
CORE_HANDLERS_PACKAGES: frozenset[str] = frozenset(
    {
        "renglo-lib",
    }
)


def normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def is_core_handlers_package(name: str) -> bool:
    n = normalize_dist_name(name)
    return any(normalize_dist_name(c) == n for c in CORE_HANDLERS_PACKAGES)
