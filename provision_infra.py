from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any

from deploy_input import load_environment_variables_from_deploy_input
from lib import get_workspace_root, get_script_dir, validate_extension_name, validate_iam_policy_file
from runtime_config import ensure_runtime_profile_file
from state_store import STATE_VERSION, get_state_paths, read_json, utc_now_iso, write_json


def _run_script(script_name: str, env: dict[str, str], extra_args: list[str] | None = None) -> int:
    script = get_script_dir() / script_name
    if not script.is_file():
        print(f"ERROR: Script not found: {script}")
        return 1
    run_env = {**os.environ, **env}
    cmd = [str(script), *(extra_args or [])]
    return subprocess.run(cmd, cwd=get_workspace_root(), env=run_env).returncode


def _read_lambda_env_vars(extension: str, workspace_root: Path) -> dict[str, Any]:
    """ECS-related env from deploy_input.lambda_config (ECR/cluster/bucket names for manifest)."""
    return load_environment_variables_from_deploy_input(extension, workspace_root)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def update_provision_manifest(extension: str, workspace_root: Path | None = None) -> Path:
    root = workspace_root or get_workspace_root()
    vars_env = _read_lambda_env_vars(extension, root)
    paths = get_state_paths(extension, root)
    previous = read_json(paths.provision_manifest) or {}
    region = os.environ.get("AWS_REGION", previous.get("aws_region", "us-east-1"))
    ecr_repo = f"{extension}-handlers-ecs"
    provision = {
        "state_version": STATE_VERSION,
        "extension": extension,
        "updated_at": utc_now_iso(),
        "aws_region": region,
        "ecr": {
            "repository": previous.get("ecr", {}).get("repository", ecr_repo),
            "image_uri": previous.get("ecr", {}).get("image_uri", f"{ecr_repo}:latest"),
        },
        "ecs": {
            "cluster": vars_env.get("ECS_CLUSTER", previous.get("ecs", {}).get("cluster", f"{extension}-handlers")),
            "task_definition": vars_env.get("ECS_TASK_DEFINITION", previous.get("ecs", {}).get("task_definition", f"{extension}-handlers-ecs")),
            "launch_type": previous.get("ecs", {}).get("launch_type", "fargate"),
            "network_mode": previous.get("ecs", {}).get("network_mode", "awsvpc"),
            "subnets": _split_csv(vars_env.get("ECS_SUBNETS")) or previous.get("ecs", {}).get("subnets", []),
            "security_groups": _split_csv(vars_env.get("ECS_SECURITY_GROUPS")) or previous.get("ecs", {}).get("security_groups", []),
        },
        "buckets": {
            "ecs_results_bucket": vars_env.get("ECS_RESULTS_BUCKET", previous.get("buckets", {}).get("ecs_results_bucket", f"{extension}-handlers-ecs-results"))
        },
    }
    write_json(paths.provision_manifest, provision)
    return paths.provision_manifest


def cmd_apply(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension_name(extension)
    validate_iam_policy_file(extension, root)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("--with-capacity", action="store_true")
    parsed, _ = parser.parse_known_args(args)
    env = {"EXTENSION_NAME": extension, "WORKSPACE_ROOT": str(root)}
    if parsed.profile:
        env["AWS_PROFILE"] = parsed.profile

    rc = _run_script("setup_iam_role.sh", env)
    if rc != 0:
        return rc
    if parsed.with_capacity:
        rc = _run_script("provision_ecs_capacity.sh", env)
        if rc != 0:
            return rc
    manifest_path = update_provision_manifest(extension, root)
    ensure_runtime_profile_file(extension, root)
    print(f"Provision manifest updated: {manifest_path}")
    return 0


def cmd_destroy(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension_name(extension)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parsed, _ = parser.parse_known_args(args)
    env = {"EXTENSION_NAME": extension, "WORKSPACE_ROOT": str(root)}
    if parsed.profile:
        env["AWS_PROFILE"] = parsed.profile
    rc = _run_script("undeploy_ecs_capacity.sh", env)
    if rc != 0:
        return rc
    manifest_path = update_provision_manifest(extension, root)
    print(f"Provision manifest refreshed after destroy: {manifest_path}")
    return 0

