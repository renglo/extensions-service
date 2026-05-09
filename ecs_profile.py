#!/usr/bin/env python3
"""
Per-extension ECS deploy profile: launch type (Fargate vs EC2), sizing presets, network mode.

Lives at: extensions/<name>/installer/ecs_profile.json

USER EDIT OF FINAL ECS DEPLOY CONFIG FILE AT OWN RISK. Recommended to use defualts by size.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

# Fargate-compatible CPU (units) and memory (MiB) — see AWS Fargate platform versions
SIZE_PRESETS: dict[str, dict[str, Any]] = {
    "small": {
        "task_cpu": 512,
        "task_memory": 1024,
        "ec2_instance_type": "m5.large",
        "asg_min_size": 0,
        "asg_desired_capacity": 0,
        "asg_max_size": 1,
    },
    "medium": {
        "task_cpu": 1024,
        "task_memory": 4096,
        "ec2_instance_type": "m5.xlarge",
        "asg_min_size": 0,
        "asg_desired_capacity": 1,
        "asg_max_size": 2,
    },
    "large": {
        "task_cpu": 2048,
        "task_memory": 8192,
        "ec2_instance_type": "m5.2xlarge",
        "asg_min_size": 1,
        "asg_desired_capacity": 1,
        "asg_max_size": 4,
    },
}

# Rough vCPU / MiB for optional warning only (not authoritative)
_INSTANCE_HINT: dict[str, tuple[int, int]] = {
    "m5.large": (2, 8192),
    "m5.xlarge": (4, 16384),
    "m5.2xlarge": (8, 32768),
    "m5.4xlarge": (16, 65536),
    "m6i.large": (2, 8192),
    "m6i.xlarge": (4, 16384),
    "m6i.2xlarge": (8, 32768),
    "c5.xlarge": (4, 8192),
}


def profile_path(workspace_root: str | Path, extension: str) -> Path:
    return Path(workspace_root) / "extensions" / extension / "installer" / "ecs_profile.json"


def default_profile() -> dict[str, Any]:
    p = SIZE_PRESETS["medium"].copy()
    return {
        "launch_type": "fargate",
        "size": "medium",
        "network_mode": "awsvpc",
        "task_cpu": p["task_cpu"],
        "task_memory": p["task_memory"],
        "ec2_instance_type": p["ec2_instance_type"],
        "asg_min_size": p["asg_min_size"],
        "asg_desired_capacity": p["asg_desired_capacity"],
        "asg_max_size": p["asg_max_size"],
    }


def effective_network_mode(launch_type: str, network_mode: str | None) -> str:
    lt = (launch_type or "fargate").lower()
    nm = (network_mode or "").strip().lower()
    if nm in ("", "auto"):
        return "bridge" if lt == "ec2" else "awsvpc"
    return nm


def apply_size(profile: dict[str, Any], size: str) -> None:
    s = size.lower()
    if s not in SIZE_PRESETS:
        raise ValueError(f"Unknown size {size!r}; use small, medium, or large")
    preset = SIZE_PRESETS[s]
    profile["size"] = s
    profile["task_cpu"] = preset["task_cpu"]
    profile["task_memory"] = preset["task_memory"]
    profile["ec2_instance_type"] = preset["ec2_instance_type"]
    profile["asg_min_size"] = preset["asg_min_size"]
    profile["asg_desired_capacity"] = preset["asg_desired_capacity"]
    profile["asg_max_size"] = preset["asg_max_size"]


def write_profile(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def load_profile(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def ensure_default_profile(workspace_root: str | Path, extension: str) -> Path:
    """Create ecs_profile.json with defaults if missing."""
    path = profile_path(workspace_root, extension)
    if path.is_file():
        return path
    write_profile(path, default_profile())
    print(f"Created default ECS profile: {path}")
    return path


def maybe_warn_instance_vs_task(profile: dict[str, Any]) -> None:
    inst = str(profile.get("ec2_instance_type") or "").strip()
    if not inst:
        return
    hint = _INSTANCE_HINT.get(inst)
    if not hint:
        return
    try:
        task_cpu = int(str(profile.get("task_cpu", "0")))
        task_mem = int(str(profile.get("task_memory", "0")))
    except ValueError:
        return
    inst_vcpu, inst_mib = hint
    # ECS CPU units: 1024 = 1 vCPU
    task_vcpu = task_cpu / 1024.0
    if task_vcpu > inst_vcpu + 0.01 or task_mem > inst_mib:
        print(
            f"WARNING: task asks for ~{task_vcpu:.2f} vCPU / {task_mem} MiB; "
            f"instance type {inst!r} is roughly {inst_vcpu} vCPU / {inst_mib} MiB. "
            f"Increase instance size or lower task_cpu/task_memory if tasks stay PENDING.",
            file=sys.stderr,
        )


def build_profile_for_deploy(workspace_root: str | Path, extension: str) -> dict[str, Any]:
    from state_store import get_state_paths

    state_path = get_state_paths(extension, workspace_root).runtime_profile
    raw = load_profile(state_path)
    if not raw:
        path = profile_path(workspace_root, extension)
        raw = load_profile(path)
    if not raw:
        data = default_profile()
    else:
        data = default_profile()
        for k, v in raw.items():
            data[k] = v
    lt = str(data.get("launch_type") or "fargate").lower()
    if lt not in ("fargate", "ec2"):
        lt = "fargate"
    data["launch_type"] = lt
    nm = effective_network_mode(lt, str(data.get("network_mode") or ""))
    if lt == "fargate" and nm != "awsvpc":
        print(
            "WARNING: Fargate requires awsvpc; forcing network_mode=awsvpc",
            file=sys.stderr,
        )
        nm = "awsvpc"
    data["network_mode"] = nm
    # Ensure size keys present
    size = str(data.get("size") or "medium").lower()
    if size not in SIZE_PRESETS:
        size = "medium"
    data["size"] = size
    for key in ("task_cpu", "task_memory", "ec2_instance_type"):
        if key not in data or data[key] in (None, ""):
            apply_size(data, size)
    return data


def export_for_deploy_shell(workspace_root: str, extension: str) -> None:
    data = build_profile_for_deploy(workspace_root, extension)
    lt = data["launch_type"]
    nm = data["network_mode"]
    cpu = str(data.get("task_cpu") or "1024")
    mem = str(data.get("task_memory") or "4096")
    inst = str(data.get("ec2_instance_type") or "m5.xlarge")
    asg_min = str(data.get("asg_min_size") or "0")
    asg_desired = str(data.get("asg_desired_capacity") or "0")
    asg_max = str(data.get("asg_max_size") or "1")
    print(f"export ECS_PROFILE_LAUNCH_TYPE={shlex.quote(lt)}")
    print(f"export ECS_PROFILE_NETWORK_MODE={shlex.quote(nm)}")
    print(f"export ECS_PROFILE_TASK_CPU={shlex.quote(cpu)}")
    print(f"export ECS_PROFILE_TASK_MEMORY={shlex.quote(mem)}")
    print(f"export ECS_PROFILE_EC2_INSTANCE_TYPE={shlex.quote(inst)}")
    print(f"export ECS_PROFILE_ASG_MIN_SIZE={shlex.quote(asg_min)}")
    print(f"export ECS_PROFILE_ASG_DESIRED_CAPACITY={shlex.quote(asg_desired)}")
    print(f"export ECS_PROFILE_ASG_MAX_SIZE={shlex.quote(asg_max)}")
    maybe_warn_instance_vs_task(data)


def cmd_apply(
    workspace_root: str,
    extension: str,
    *,
    size: str | None,
    launch_type: str | None,
    network_mode: str | None,
    force: bool,
) -> int:
    path = profile_path(workspace_root, extension)
    existing = load_profile(path) if path.is_file() else None
    if force:
        data = default_profile()
    elif existing:
        data = dict(existing)
    else:
        data = default_profile()

    if launch_type is not None:
        lt = launch_type.lower()
        if lt not in ("fargate", "ec2"):
            print("ERROR: --launch-type must be fargate or ec2", file=sys.stderr)
            return 1
        data["launch_type"] = lt
        if network_mode is None:
            data["network_mode"] = effective_network_mode(lt, "auto")

    if network_mode is not None:
        nm = network_mode.lower()
        if nm not in ("awsvpc", "bridge", "host"):
            print("ERROR: --network-mode must be awsvpc, bridge, or host", file=sys.stderr)
            return 1
        data["network_mode"] = nm

    if size is not None:
        try:
            apply_size(data, size)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    # Normalize after updates
    lt = str(data.get("launch_type") or "fargate").lower()
    nm_raw = str(data.get("network_mode") or "")
    nm = effective_network_mode(lt, nm_raw)
    if lt == "fargate" and nm != "awsvpc":
        print("WARNING: Fargate requires awsvpc; setting network_mode=awsvpc", file=sys.stderr)
        nm = "awsvpc"
    data["launch_type"] = lt
    data["network_mode"] = nm
    sz = str(data.get("size") or "medium").lower()
    if sz not in SIZE_PRESETS:
        sz = "medium"
        data["size"] = sz
    preset = SIZE_PRESETS[sz]
    for key in (
        "task_cpu",
        "task_memory",
        "ec2_instance_type",
        "asg_min_size",
        "asg_desired_capacity",
        "asg_max_size",
    ):
        if key not in data or data[key] in (None, ""):
            data[key] = preset[key]

    write_profile(path, data)
    print(f"Wrote {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ECS extension profile helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export-for-deploy", help="Print shell exports for deploy_ecs.sh")
    p_export.add_argument("workspace_root")
    p_export.add_argument("extension")

    p_apply = sub.add_parser("apply", help="Create/update ecs_profile.json (merge unless --force)")
    p_apply.add_argument("workspace_root")
    p_apply.add_argument("extension")
    p_apply.add_argument("--size", choices=sorted(SIZE_PRESETS.keys()))
    p_apply.add_argument("--launch-type", choices=("fargate", "ec2"))
    p_apply.add_argument("--network-mode", choices=("awsvpc", "bridge", "host"))
    p_apply.add_argument("--force", action="store_true", help="Reset to defaults then apply flags")

    g = p_apply.add_mutually_exclusive_group()
    g.add_argument("--small", action="store_true", help="Preset size small")
    g.add_argument("--medium", action="store_true", help="Preset size medium")
    g.add_argument("--large", action="store_true", help="Preset size large")

    args = ap.parse_args()
    if args.cmd == "export-for-deploy":
        export_for_deploy_shell(args.workspace_root, args.extension)
        return 0
    if args.cmd == "apply":
        size = args.size
        if args.small:
            size = "small"
        elif args.medium:
            size = "medium"
        elif args.large:
            size = "large"
        return cmd_apply(
            args.workspace_root,
            args.extension,
            size=size,
            launch_type=args.launch_type,
            network_mode=args.network_mode,
            force=args.force,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
