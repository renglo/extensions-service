"""WORKSPACE_ROOT override so compose can point builds at an isolated tree."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from lib import get_workspace_root  # noqa: E402
from state_store import (  # noqa: E402
    default_repo_root,
    get_extensions_service_root,
    get_state_paths,
    resolve_state_dir,
)


def test_workspace_root_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    assert get_workspace_root() == tmp_path.resolve()


def test_workspace_root_default_is_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    expected = Path(_SERVICE_ROOT).resolve().parent.parent
    assert get_workspace_root() == expected


def test_state_dir_on_the_clone_stays_next_to_the_service() -> None:
    clone = default_repo_root()
    state = resolve_state_dir("arbitium", clone)
    assert state == get_extensions_service_root() / "state" / "arbitium"


def test_state_dir_on_an_isolated_workspace_stays_in_that_tree(tmp_path: Path) -> None:
    state = resolve_state_dir("arbitium", tmp_path)
    assert state == tmp_path.resolve() / "dev" / "extensions-service" / "state" / "arbitium"
    assert state.is_relative_to(tmp_path.resolve())
    assert get_extensions_service_root().resolve() not in state.parents


def test_get_state_paths_on_the_clone_uses_service_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    paths = get_state_paths("arbitium")
    assert paths.state_dir == get_extensions_service_root() / "state" / "arbitium"


def test_get_state_paths_follows_workspace_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    paths = get_state_paths("arbitium")
    assert paths.root == tmp_path.resolve()
    assert paths.state_dir == tmp_path.resolve() / "dev" / "extensions-service" / "state" / "arbitium"
    assert paths.lambda_deployment_zip == paths.state_dir / "lambda_deployment.zip"
