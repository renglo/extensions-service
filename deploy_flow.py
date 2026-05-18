from __future__ import annotations

import argparse
import os
import subprocess

from deploy_input import (
    get_ecr_image_uri_from_deploy_input,
    get_runtime_env_from_deploy_input,
    resolve_deploy_input_file,
)
from lib import get_workspace_root, get_script_dir, merge_script_env, validate_extension
from state_store import STATE_VERSION, default_release_manifest, get_state_paths, read_json, utc_now_iso, write_json


def _run_script(script_name: str, env: dict[str, str], extra_args: list[str] | None = None) -> int:
    script = get_script_dir() / script_name
    if not script.is_file():
        print(f"ERROR: Script not found: {script}")
        return 1
    run_env = merge_script_env(env)
    cmd = [str(script), *(extra_args or [])]
    return subprocess.run(cmd, cwd=get_workspace_root(), env=run_env).returncode


def _load_release_manifest(extension: str):
    paths = get_state_paths(extension)
    data = read_json(paths.release_manifest) or default_release_manifest(extension)
    return paths, data


def _build_single(extension: str, root, large: bool, local: bool) -> int:
    """Run one build pass and update the release manifest. Returns the script exit code."""
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(root),
        "EXTENSION_SERVICE_NATIVE_PLATFORM": "1" if local else "0",
        "EXTENSION_SERVICE_LARGE_BUILD": "1" if large else "0",
    }
    rc = _run_script("build_lambda_package.sh", env=env)
    if rc != 0:
        return rc
    paths, manifest = _load_release_manifest(extension)
    mode = "ecs-large" if large else "lambda"
    if mode == "ecs-large":
        image = f"{extension}-ecs-builder:{'local' if local else 'latest'}"
    else:
        image = f"{extension}-lambda-builder:{'local' if local else 'latest'}"
    manifest["state_version"] = STATE_VERSION
    manifest["updated_at"] = utc_now_iso()
    manifest.setdefault("builds", {})
    manifest["builds"][mode] = {
        "image": image,
        "platform": "linux/arm64" if local else "linux/amd64",
        "created_at": utc_now_iso(),
    }
    # Keep last_build pointing to the most recently completed build
    manifest["last_build"] = {**manifest["builds"][mode], "mode": mode}
    write_json(paths.release_manifest, manifest)
    print(f"Release manifest updated ({mode}): {paths.release_manifest}")
    return 0


def _ecs_is_provisioned(extension: str) -> bool:
    """Return True if ECS infra is configured — from provision_manifest OR deploy_input."""
    paths = get_state_paths(extension)
    manifest = read_json(paths.provision_manifest)
    if manifest and manifest.get("ecs", {}).get("cluster"):
        return True
    runtime_env = get_runtime_env_from_deploy_input(extension)
    if runtime_env.get("ECS_CLUSTER"):
        return True
    return False


def cmd_build(extension: str, args: list[str]) -> int:
    """Build artifacts for the extension.

    Lambda zip is always built first. ECS image is also built when:
      - provision_manifest.json exists with an ECS cluster (auto-detected), OR
      - --large is passed explicitly (useful before provision-infra has run).

    Flags:
      --large     Force ECS image build in addition to Lambda zip.
      --local     Build Lambda as ARM64 (for run-local). ECS image is always amd64.
      --no-ecs    Skip ECS image build even when provision_manifest says ECS is provisioned.
    """
    root = get_workspace_root()
    validate_extension(extension, root)

    force_large = "--large" in args
    skip_ecs = "--no-ecs" in args
    build_local = "--local" in args

    # Lambda zip is always the first artifact
    print("==> Building Lambda zip...")
    rc = _build_single(extension, root, large=False, local=build_local)
    if rc != 0:
        return rc

    # ECS image: explicit flag OR auto-detected from provision_manifest / deploy_input
    if not skip_ecs and (force_large or _ecs_is_provisioned(extension)):
        source = "--large flag" if force_large else "deploy_input / provision_manifest"
        print(f"==> Building ECS image (detected from {source})...")
        return _build_single(extension, root, large=True, local=False)

    return 0


def cmd_push(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension(extension, root)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parsed, remaining = parser.parse_known_args(args)
    env = {"EXTENSION_NAME": extension, "WORKSPACE_ROOT": str(root)}
    paths = get_state_paths(extension)

    # Primary source: provision_manifest.json (from provision-infra apply)
    provision = read_json(paths.provision_manifest) or {}
    ecs = provision.get("ecs") or {}
    buckets = provision.get("buckets") or {}

    # Fallback source: deploy_input.json (VARS / SECRETS)
    ecs_env = get_runtime_env_from_deploy_input(extension, root)

    def _first(*values: str) -> str | None:
        return next((v for v in values if v), None)

    aws_region = _first(provision.get("aws_region"), ecs_env.get("AWS_REGION"))
    ecs_results_bucket = _first(buckets.get("ecs_results_bucket"), ecs_env.get("ECS_RESULTS_BUCKET"))
    ecs_cluster = _first(ecs.get("cluster"), ecs_env.get("ECS_CLUSTER"))
    ecs_task_def = _first(ecs.get("task_definition"), ecs_env.get("ECS_TASK_DEFINITION"))

    if aws_region:
        env["AWS_REGION"] = aws_region
    if ecs_results_bucket:
        env["ECS_RESULTS_BUCKET"] = ecs_results_bucket
    if ecs_cluster:
        env["ECS_CLUSTER"] = ecs_cluster
    if ecs_task_def:
        env["ECS_TASK_DEFINITION"] = ecs_task_def

    # ECS launch profile: use file if present, otherwise inject key vars from deploy_input VARS
    if paths.runtime_profile.is_file():
        env["ECS_PROFILE_FILE"] = str(paths.runtime_profile)
    else:
        if ecs_env.get("ECS_LAUNCH_TYPE"):
            env["ECS_PROFILE_LAUNCH_TYPE"] = str(ecs_env["ECS_LAUNCH_TYPE"])
        if ecs_env.get("ECS_NETWORK_MODE"):
            env["ECS_PROFILE_NETWORK_MODE"] = str(ecs_env["ECS_NETWORK_MODE"])

    deploy_input_file = resolve_deploy_input_file(extension, root)
    if deploy_input_file is not None:
        env["DEPLOY_INPUT_FILE"] = str(deploy_input_file)
    if parsed.profile:
        env["AWS_PROFILE"] = parsed.profile

    rc = _run_script("deploy_ecs.sh", env=env, extra_args=remaining)
    if rc != 0:
        return rc

    paths = get_state_paths(extension)
    provision = read_json(paths.provision_manifest) or {}
    release = read_json(paths.release_manifest) or default_release_manifest(extension)
    image_uri = (
        ((provision.get("ecr") or {}).get("image_uri"))
        or get_ecr_image_uri_from_deploy_input(extension, root)
        or f"{extension}-handlers-ecs:latest"
    )
    release["state_version"] = STATE_VERSION
    release["updated_at"] = utc_now_iso()
    release["last_push"] = {"image_uri": image_uri, "created_at": utc_now_iso()}
    write_json(paths.release_manifest, release)
    print(f"Release manifest updated: {paths.release_manifest}")
    return 0


def cmd_publish(extension: str, args: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--type", default="ecs")
    parsed, _ = parser.parse_known_args(args)
    paths = get_state_paths(extension)
    release = read_json(paths.release_manifest) or default_release_manifest(extension)
    release["state_version"] = STATE_VERSION
    release["updated_at"] = utc_now_iso()
    release["last_publish"] = {"target": parsed.type, "created_at": utc_now_iso()}
    write_json(paths.release_manifest, release)
    print(f"Release publish recorded in: {paths.release_manifest}")
    return 0

