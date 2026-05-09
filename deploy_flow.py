from __future__ import annotations

import argparse
import os
import subprocess

from deploy_input import resolve_deploy_input_file
from lib import get_workspace_root, get_script_dir, validate_extension
from state_store import STATE_VERSION, default_release_manifest, get_state_paths, read_json, utc_now_iso, write_json


def _run_script(script_name: str, env: dict[str, str], extra_args: list[str] | None = None) -> int:
    script = get_script_dir() / script_name
    if not script.is_file():
        print(f"ERROR: Script not found: {script}")
        return 1
    run_env = {**os.environ, **env}
    cmd = [str(script), *(extra_args or [])]
    return subprocess.run(cmd, cwd=get_workspace_root(), env=run_env).returncode


def _load_release_manifest(extension: str):
    paths = get_state_paths(extension)
    data = read_json(paths.release_manifest) or default_release_manifest(extension)
    return paths, data


def cmd_build(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension(extension, root)
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(root),
        "EXTENSION_SERVICE_NATIVE_PLATFORM": "1" if "--local" in args else "0",
        "EXTENSION_SERVICE_LARGE_BUILD": "1" if "--large" in args else "0",
    }
    rc = _run_script("build_lambda_package.sh", env=env)
    if rc != 0:
        return rc
    paths, manifest = _load_release_manifest(extension)
    mode = "ecs-large" if env["EXTENSION_SERVICE_LARGE_BUILD"] == "1" else "lambda"
    image = f"{extension}-ecs-builder:{'local' if env['EXTENSION_SERVICE_NATIVE_PLATFORM'] == '1' and mode == 'ecs-large' else 'latest'}" if mode == "ecs-large" else f"{extension}-lambda-builder:{'local' if env['EXTENSION_SERVICE_NATIVE_PLATFORM'] == '1' else 'latest'}"
    manifest["state_version"] = STATE_VERSION
    manifest["updated_at"] = utc_now_iso()
    manifest["last_build"] = {
        "mode": mode,
        "image": image,
        "platform": "linux/arm64" if env["EXTENSION_SERVICE_NATIVE_PLATFORM"] == "1" else "linux/amd64",
        "created_at": utc_now_iso(),
    }
    write_json(paths.release_manifest, manifest)
    print(f"Release manifest updated: {paths.release_manifest}")
    return 0


def cmd_push(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension(extension, root)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parsed, remaining = parser.parse_known_args(args)
    env = {"EXTENSION_NAME": extension, "WORKSPACE_ROOT": str(root)}
    paths = get_state_paths(extension)
    provision = read_json(paths.provision_manifest) or {}
    ecs = provision.get("ecs") or {}
    buckets = provision.get("buckets") or {}
    if provision.get("aws_region"):
        env["AWS_REGION"] = str(provision["aws_region"])
    if buckets.get("ecs_results_bucket"):
        env["ECS_RESULTS_BUCKET"] = str(buckets["ecs_results_bucket"])
    if ecs.get("cluster"):
        env["ECS_CLUSTER"] = str(ecs["cluster"])
    if ecs.get("task_definition"):
        env["ECS_TASK_DEFINITION"] = str(ecs["task_definition"])
    if paths.runtime_profile.is_file():
        env["ECS_PROFILE_FILE"] = str(paths.runtime_profile)
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
    image_uri = (((provision.get("ecr") or {}).get("image_uri")) or f"{extension}-handlers-ecs:latest")
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

