#!/usr/bin/env python3
"""
Resolve ECS launch profile for deploy push (stage 2).

Priority (stage 2 does not read runtime_profile.json — that is stage 3):
  1. provision_manifest.json → ecs.launch_type, ecs.network_mode
  2. deploy_input.json VARS → ECS_LAUNCH_TYPE, ECS_NETWORK_MODE
  3. AWS CLI (cluster capacity / container instances, latest task definition)
  4. Smart default (ec2+bridge if cluster looks EC2, else fargate+awsvpc)
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deploy_input import get_runtime_env_from_deploy_input
from ecs_profile import effective_network_mode
from lib import get_workspace_root
from state_store import get_state_paths, read_json


@dataclass
class ResolvedDeployProfile:
    launch_type: str
    network_mode: str
    task_cpu: int
    task_memory: int
    sources: list[str]


def _aws_json(args: list[str]) -> dict[str, Any] | list[Any] | None:
    try:
        proc = subprocess.run(
            ["aws", *args, "--output", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        return None


def _normalize_launch_type(value: str | None) -> str | None:
    if not value:
        return None
    lt = value.strip().lower()
    if lt in ("fargate", "ec2"):
        return lt
    return None


def _normalize_network_mode(value: str | None) -> str | None:
    if not value:
        return None
    nm = value.strip().lower()
    if nm in ("awsvpc", "bridge", "host"):
        return nm
    return None


def _profile_from_manifest(extension: str, workspace_root: Path) -> tuple[str | None, str | None, list[str]]:
    manifest = read_json(get_state_paths(extension, workspace_root).provision_manifest) or {}
    ecs = manifest.get("ecs") or {}
    lt = _normalize_launch_type(str(ecs.get("launch_type") or ""))
    nm = _normalize_network_mode(str(ecs.get("network_mode") or ""))
    sources: list[str] = []
    if lt:
        sources.append("provision_manifest.launch_type")
    if nm:
        sources.append("provision_manifest.network_mode")
    return lt, nm, sources


def _profile_from_deploy_input(extension: str, workspace_root: Path) -> tuple[str | None, str | None, list[str]]:
    env = get_runtime_env_from_deploy_input(extension, workspace_root)
    lt = _normalize_launch_type(env.get("ECS_LAUNCH_TYPE"))
    nm = _normalize_network_mode(env.get("ECS_NETWORK_MODE"))
    sources: list[str] = []
    if lt:
        sources.append("deploy_input.ECS_LAUNCH_TYPE")
    if nm:
        sources.append("deploy_input.ECS_NETWORK_MODE")
    return lt, nm, sources


def _detect_launch_type_from_cluster(cluster: str, region: str) -> tuple[str | None, list[str]]:
    sources: list[str] = []
    instances = _aws_json(
        [
            "ecs",
            "list-container-instances",
            "--cluster",
            cluster,
            "--region",
            region,
            "--query",
            "containerInstanceArns",
        ]
    )
    if isinstance(instances, list) and len(instances) > 0:
        return "ec2", ["aws:ecs.list-container-instances"]

    cluster_info = _aws_json(
        [
            "ecs",
            "describe-clusters",
            "--clusters",
            cluster,
            "--region",
            region,
            "--include",
            "ATTACHMENTS",
            "--query",
            "clusters[0]",
        ]
    )
    if not isinstance(cluster_info, dict):
        return None, sources

    cps = cluster_info.get("capacityProviders") or []
    fargate_only = True
    has_ec2_cp = False
    for cp_name in cps:
        name = str(cp_name).upper()
        if name in ("FARGATE", "FARGATE_SPOT"):
            continue
        fargate_only = False
        has_ec2_cp = True

    if has_ec2_cp:
        return "ec2", ["aws:ecs.describe-clusters.capacityProviders"]

    strategy = cluster_info.get("defaultCapacityProviderStrategy") or []
    for entry in strategy:
        cp = str((entry or {}).get("capacityProvider") or "").upper()
        if cp and cp not in ("FARGATE", "FARGATE_SPOT"):
            return "ec2", ["aws:ecs.describe-clusters.defaultCapacityProviderStrategy"]

    if cps and fargate_only:
        return "fargate", ["aws:ecs.describe-clusters.capacityProviders"]

    return None, sources


def _detect_from_task_definition(
    task_family: str, region: str
) -> tuple[str | None, str | None, int | None, int | None, list[str]]:
    sources: list[str] = []
    td = _aws_json(
        [
            "ecs",
            "describe-task-definition",
            "--task-definition",
            task_family,
            "--region",
            region,
            "--query",
            "taskDefinition",
        ]
    )
    if not isinstance(td, dict):
        return None, None, None, None, sources

    lt: str | None = None
    compat = [str(c).upper() for c in (td.get("requiresCompatibilities") or [])]
    if "FARGATE" in compat:
        lt = "fargate"
        sources.append("aws:ecs.describe-task-definition.compatibilities")
    elif "EC2" in compat:
        lt = "ec2"
        sources.append("aws:ecs.describe-task-definition.compatibilities")

    nm = _normalize_network_mode(str(td.get("networkMode") or ""))
    if nm:
        sources.append("aws:ecs.describe-task-definition.networkMode")

    cpu: int | None = None
    memory: int | None = None
    try:
        if td.get("cpu") not in (None, ""):
            cpu = int(str(td["cpu"]))
            sources.append("aws:ecs.describe-task-definition.cpu")
    except ValueError:
        pass
    try:
        if td.get("memory") not in (None, ""):
            memory = int(str(td["memory"]))
            sources.append("aws:ecs.describe-task-definition.memory")
    except ValueError:
        pass

    return lt, nm, cpu, memory, sources


def resolve_for_push(
    extension: str,
    workspace_root: Path | None,
    *,
    aws_region: str,
    ecs_cluster: str,
    ecs_task_family: str,
) -> ResolvedDeployProfile:
    root = workspace_root or get_workspace_root()
    sources: list[str] = []

    launch_type: str | None = None
    network_mode: str | None = None
    task_cpu: int | None = None
    task_memory: int | None = None

    lt_m, nm_m, src_m = _profile_from_manifest(extension, root)
    sources.extend(src_m)
    launch_type = launch_type or lt_m
    network_mode = network_mode or nm_m

    lt_d, nm_d, src_d = _profile_from_deploy_input(extension, root)
    sources.extend(src_d)
    launch_type = launch_type or lt_d
    network_mode = network_mode or nm_d

    if not launch_type:
        lt_c, src_c = _detect_launch_type_from_cluster(ecs_cluster, aws_region)
        if lt_c:
            launch_type = lt_c
            sources.extend(src_c)

    td_lt, td_nm, td_cpu, td_mem, src_td = _detect_from_task_definition(ecs_task_family, aws_region)
    sources.extend(src_td)
    launch_type = launch_type or td_lt
    network_mode = network_mode or td_nm
    task_cpu = task_cpu or td_cpu
    task_memory = task_memory or td_mem

    if not launch_type:
        cluster_looks_ec2 = any(
            "list-container-instances" in s
            or "capacityProviders" in s
            or "defaultCapacityProviderStrategy" in s
            for s in sources
        )
        if cluster_looks_ec2:
            launch_type = "ec2"
            sources.append("default:cluster-ec2")
        else:
            launch_type = "fargate"
            sources.append("default:fargate")
        print(
            f"WARNING: ECS launch_type not in manifest/deploy_input; inferred {launch_type!r} from AWS/default.",
            file=sys.stderr,
        )

    network_mode = effective_network_mode(launch_type, network_mode or None)
    if not any("network_mode" in s or "ECS_NETWORK_MODE" in s for s in sources):
        if launch_type == "ec2" and network_mode == "bridge":
            sources.append("default:ec2-bridge")
        elif launch_type == "fargate" and network_mode == "awsvpc":
            sources.append("default:fargate-awsvpc")

    if launch_type == "fargate" and network_mode != "awsvpc":
        network_mode = "awsvpc"
        sources.append("constraint:fargate-requires-awsvpc")

    task_cpu = task_cpu or 1024
    task_memory = task_memory or 4096

    return ResolvedDeployProfile(
        launch_type=launch_type,
        network_mode=network_mode,
        task_cpu=task_cpu,
        task_memory=task_memory,
        sources=sources,
    )


def export_shell_lines(profile: ResolvedDeployProfile) -> str:
    lines = [
        f"ECS_PROFILE_LAUNCH_TYPE={shlex.quote(profile.launch_type)}",
        f"ECS_PROFILE_NETWORK_MODE={shlex.quote(profile.network_mode)}",
        f"ECS_PROFILE_TASK_CPU={shlex.quote(str(profile.task_cpu))}",
        f"ECS_PROFILE_TASK_MEMORY={shlex.quote(str(profile.task_memory))}",
        f"ECS_PROFILE_SOURCES={shlex.quote(','.join(profile.sources))}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve ECS profile for deploy push (stage 2)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export-shell", help="Print shell assignments for deploy_ecs.sh")
    p_export.add_argument("workspace_root")
    p_export.add_argument("extension")
    p_export.add_argument("--region", default="us-east-1")
    p_export.add_argument("--cluster", required=True)
    p_export.add_argument("--task-family", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "export-shell":
        profile = resolve_for_push(
            args.extension,
            Path(args.workspace_root),
            aws_region=args.region,
            ecs_cluster=args.cluster,
            ecs_task_family=args.task_family,
        )
        sys.stdout.write(export_shell_lines(profile))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
