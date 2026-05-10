from __future__ import annotations

import argparse
from pathlib import Path

from lib import get_workspace_root, validate_extension_name
from state_store import (
    STATE_VERSION,
    default_runtime_profile,
    get_state_paths,
    read_json,
    utc_now_iso,
    write_json,
)


SIZE_PRESETS = {
    "small": {"task_cpu": 512, "task_memory": 1024, "ec2_instance_type": "m5.large", "asg_min_size": 0, "asg_desired_capacity": 0, "asg_max_size": 1},
    "medium": {"task_cpu": 1024, "task_memory": 4096, "ec2_instance_type": "m5.xlarge", "asg_min_size": 0, "asg_desired_capacity": 1, "asg_max_size": 2},
    "large": {"task_cpu": 2048, "task_memory": 8192, "ec2_instance_type": "m5.2xlarge", "asg_min_size": 1, "asg_desired_capacity": 1, "asg_max_size": 4},
}


def _effective_network_mode(launch_type: str, network_mode: str | None) -> str:
    nm = (network_mode or "").strip().lower()
    if not nm or nm == "auto":
        return "bridge" if launch_type == "ec2" else "awsvpc"
    return nm


def _load_profile(extension: str) -> tuple:
    paths = get_state_paths(extension)
    profile = read_json(paths.runtime_profile) or default_runtime_profile()
    return paths, profile


def ensure_runtime_profile_file(extension: str, workspace_root: Path | None = None) -> Path:
    """Create state/<ext>/runtime_profile.json with defaults if missing."""
    root = workspace_root or get_workspace_root()
    paths = get_state_paths(extension, root)
    if not paths.runtime_profile.is_file():
        write_json(paths.runtime_profile, default_runtime_profile())
    return paths.runtime_profile


def cmd_set_profile(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension_name(extension)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--small", action="store_true")
    parser.add_argument("--medium", action="store_true")
    parser.add_argument("--large", action="store_true")
    parser.add_argument("--launch-type", choices=["fargate", "ec2"])
    parser.add_argument("--network-mode")
    parser.add_argument("--task-cpu", type=int)
    parser.add_argument("--task-memory", type=int)
    parser.add_argument("--ec2-instance-type")
    parser.add_argument("--asg-min-size", type=int)
    parser.add_argument("--asg-desired-capacity", type=int)
    parser.add_argument("--asg-max-size", type=int)
    parsed, _ = parser.parse_known_args(args)

    paths, profile = _load_profile(extension)
    size = "large" if parsed.large else "small" if parsed.small else "medium" if parsed.medium else None
    if size:
        profile["size"] = size
        profile.update(SIZE_PRESETS[size])
    if parsed.launch_type:
        profile["launch_type"] = parsed.launch_type
    if parsed.task_cpu is not None:
        profile["task_cpu"] = parsed.task_cpu
    if parsed.task_memory is not None:
        profile["task_memory"] = parsed.task_memory
    if parsed.ec2_instance_type:
        profile["ec2_instance_type"] = parsed.ec2_instance_type
    if parsed.asg_min_size is not None:
        profile["asg_min_size"] = parsed.asg_min_size
    if parsed.asg_desired_capacity is not None:
        profile["asg_desired_capacity"] = parsed.asg_desired_capacity
    if parsed.asg_max_size is not None:
        profile["asg_max_size"] = parsed.asg_max_size
    profile["network_mode"] = _effective_network_mode(profile.get("launch_type", "fargate"), parsed.network_mode or profile.get("network_mode"))
    profile["state_version"] = STATE_VERSION
    profile["updated_at"] = utc_now_iso()
    write_json(paths.runtime_profile, profile)
    print(f"Runtime profile updated: {paths.runtime_profile}")
    return 0


def cmd_export_lambda_env(extension: str, _args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension_name(extension)
    paths = get_state_paths(extension)
    provision = read_json(paths.provision_manifest) or {}
    profile = read_json(paths.runtime_profile) or default_runtime_profile()
    ecs = provision.get("ecs") or {}
    buckets = provision.get("buckets") or {}

    payload = {
        "state_version": STATE_VERSION,
        "extension": extension,
        "updated_at": utc_now_iso(),
        "environment": {
            "ECS_RESULTS_BUCKET": buckets.get("ecs_results_bucket", f"{extension}-handlers-ecs-results"),
            "ECS_CLUSTER": ecs.get("cluster", f"{extension}-handlers"),
            "ECS_TASK_DEFINITION": ecs.get("task_definition", f"{extension}-handlers-ecs"),
            "ECS_SUBNETS": ",".join(ecs.get("subnets") or []),
            "ECS_SECURITY_GROUPS": ",".join(ecs.get("security_groups") or []),
            "ECS_LAUNCH_TYPE": profile.get("launch_type", "fargate"),
            "ECS_NETWORK_MODE": profile.get("network_mode", "awsvpc"),
        },
    }
    write_json(paths.lambda_env_export, payload)
    print(f"Lambda env export written: {paths.lambda_env_export}")
    return 0

