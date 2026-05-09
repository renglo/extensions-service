from __future__ import annotations

import os
from pathlib import Path

from lib import get_workspace_root
from state_store import get_state_paths, read_json


def resolve_deploy_input_file(extension: str, workspace_root: Path | None = None) -> Path | None:
    """
    Return path to deploy input JSON.
    Resolution order: DEPLOY_INPUT_FILE env, then state/<ext>/deploy_input.json if it exists.
    """
    root = workspace_root or get_workspace_root()
    env_path = os.environ.get("DEPLOY_INPUT_FILE", "").strip()
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p.resolve()
        return None
    paths = get_state_paths(extension, root)
    if paths.deploy_input.is_file():
        return paths.deploy_input
    return None


def deploy_input_from_path(path: Path) -> dict:
    payload = read_json(path)
    if payload is None:
        raise FileNotFoundError(path)
    if "lambda_config" not in payload:
        raise ValueError(f"{path} must contain lambda_config")
    return payload


def load_lambda_config_from_deploy_input(extension: str, workspace_root: Path | None = None) -> dict | None:
    p = resolve_deploy_input_file(extension, workspace_root)
    if p is None:
        return None
    payload = read_json(p)
    if not payload:
        return None
    cfg = payload.get("lambda_config")
    return cfg if isinstance(cfg, dict) else None


def load_environment_variables_from_deploy_input(extension: str, workspace_root: Path | None = None) -> dict:
    lc = load_lambda_config_from_deploy_input(extension, workspace_root)
    if not lc:
        return {}
    return (lc.get("Environment") or {}).get("Variables") or {}
