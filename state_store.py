from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import get_workspace_root


STATE_VERSION = 1


def get_extensions_service_root() -> Path:
    """
    Directory of this package (the folder containing state_store.py), e.g. .../extensions-service/.
    State files live under <this>/state/<extension>/ regardless of whether the package sits under
    dev/, infra-installer/, or elsewhere in the repo tree.
    """
    return Path(__file__).resolve().parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ExtensionStatePaths:
    root: Path
    extension: str
    state_dir: Path
    provision_manifest: Path
    runtime_profile: Path
    release_manifest: Path
    lambda_env_export: Path
    deploy_input: Path


def get_state_paths(extension: str, workspace_root: Path | None = None) -> ExtensionStatePaths:
    root = workspace_root or get_workspace_root()
    state_dir = get_extensions_service_root() / "state" / extension
    return ExtensionStatePaths(
        root=root,
        extension=extension,
        state_dir=state_dir,
        provision_manifest=state_dir / "provision_manifest.json",
        runtime_profile=state_dir / "runtime_profile.json",
        release_manifest=state_dir / "release_manifest.json",
        lambda_env_export=state_dir / "lambda_env_export.json",
        deploy_input=state_dir / "deploy_input.json",
    )


def ensure_state_dir(paths: ExtensionStatePaths) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def default_runtime_profile() -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "updated_at": utc_now_iso(),
        "launch_type": "fargate",
        "size": "medium",
        "network_mode": "awsvpc",
        "task_cpu": 1024,
        "task_memory": 4096,
        "ec2_instance_type": "m5.xlarge",
        "asg_min_size": 0,
        "asg_desired_capacity": 1,
        "asg_max_size": 2,
    }


def default_release_manifest(extension: str) -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "extension": extension,
        "updated_at": utc_now_iso(),
        "last_build": None,
        "last_push": None,
        "last_publish": None,
    }


def read_provision_manifest(extension: str, workspace_root: Path | None = None) -> dict[str, Any] | None:
    """Return parsed provision_manifest.json for extension, or None if not present."""
    paths = get_state_paths(extension, workspace_root)
    return read_json(paths.provision_manifest)


def read_runtime_profile(extension: str, workspace_root: Path | None = None) -> dict[str, Any] | None:
    """Return parsed runtime_profile.json for extension, or None if not present."""
    paths = get_state_paths(extension, workspace_root)
    return read_json(paths.runtime_profile)

