from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from install_handler_package_deps import (  # noqa: E402
    _requirement_name,
    bundled_dist_names,
    deps_to_install,
    normalize_dist_name,
    project_dist_name,
)


def _write_pyproject(package_dir: Path, name: str, dependencies: list[str]) -> None:
    package_dir.mkdir(parents=True, exist_ok=True)
    deps_lines = "\n".join(f'    "{dep}",' for dep in dependencies)
    package_dir.joinpath("pyproject.toml").write_text(
        f"""[project]
name = "{name}"
dependencies = [
{deps_lines}
]
""",
        encoding="utf-8",
    )


def test_normalize_dist_name() -> None:
    assert normalize_dist_name("arbitiumlab") == "arbitiumlab"
    assert normalize_dist_name("renglo_gro") == "renglo-gro"
    assert _requirement_name("arbitiumlab>=0.0.1") == "arbitiumlab"
    assert _requirement_name("renglo-gro==0.0.1") == "renglo-gro"


def test_deps_to_install_skips_bundled_siblings(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    triage = tmp_path / "triage"
    gro = tmp_path / "gro"
    _write_pyproject(lab, "arbitiumlab", ["requests>=2.32.0"])
    _write_pyproject(
        triage,
        "arbitiumtriage",
        ["renglo-gro>=0.0.1", "arbitiumlab>=0.0.1", "flask>=3.0.0"],
    )
    _write_pyproject(gro, "renglo-gro", ["graphforge>=0.4.0"])

    install, skipped = deps_to_install(triage, [lab, triage, gro])
    assert "flask>=3.0.0" in install
    assert "renglo-gro>=0.0.1" in skipped
    assert "arbitiumlab>=0.0.1" in skipped
    assert "requests>=2.32.0" not in install


def test_bundled_dist_names_includes_all_packages(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    gro = tmp_path / "gro"
    _write_pyproject(lab, "arbitiumlab", [])
    _write_pyproject(gro, "renglo-gro", [])
    names = bundled_dist_names([lab, gro], always_skip=frozenset({"renglo-lib"}))
    assert names == {"renglo-lib", "arbitiumlab", "renglo-gro"}


def test_project_dist_name_missing_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    bad.joinpath("pyproject.toml").write_text("[project]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        project_dist_name(bad)
