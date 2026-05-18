from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from deploy_input import load_lambda_config_from_deploy_input
from lib import (
    build_handlers_lambda_manifest_block,
    get_workspace_root,
    get_script_dir,
    merge_script_env,
    validate_extension_name,
)
from runtime_config import cmd_export_lambda_env, cmd_set_profile, ensure_runtime_profile_file
from state_store import STATE_VERSION, get_state_paths, read_json, utc_now_iso, write_json


def _run_script(script_name: str, env: dict[str, str], extra_args: list[str] | None = None) -> int:
    script = get_script_dir() / script_name
    if not script.is_file():
        print(f"ERROR: Script not found: {script}")
        return 1
    run_env = merge_script_env(env)
    cmd = [str(script), *(extra_args or [])]
    return subprocess.run(cmd, cwd=get_workspace_root(), env=run_env).returncode


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _resolve_aws_account_id(aws_profile: str | None, aws_region: str) -> str:
    import boto3

    session = (
        boto3.Session(profile_name=aws_profile, region_name=aws_region)
        if aws_profile
        else boto3.Session(region_name=aws_region)
    )
    account_id = session.client("sts").get_caller_identity().get("Account")
    if not account_id:
        raise RuntimeError("Could not resolve AWS account ID from sts:GetCallerIdentity")
    return str(account_id)


def _attach_lambda_block(
    provision: dict[str, Any],
    handlers_lambda: dict[str, str] | None,
    previous: dict[str, Any],
) -> None:
    if handlers_lambda:
        provision["lambda"] = handlers_lambda
    elif previous.get("lambda"):
        provision["lambda"] = dict(previous["lambda"])


def update_provision_manifest(
    extension: str,
    workspace_root: Path | None = None,
    infra_output: dict[str, Any] | None = None,
    launch_type: str | None = None,
    subnets: list[str] | None = None,
    security_groups: list[str] | None = None,
    lambda_only: bool = False,
    handlers_lambda: dict[str, str] | None = None,
) -> Path:
    """Write state/<ext>/provision_manifest.json from provisioned resource values.

    When lambda_only=True and the previous manifest has no ECS cluster, writes a
    minimal manifest without ECS/ECR/buckets sections. If ECS was already provisioned
    in a previous apply (manifest has ecs.cluster), those sections are always preserved —
    lambda_only re-apply never downgrades an existing ECS setup.

    Resource names are derived deterministically from the extension name.
    Values from infra_output (written by provision_ecs_infra.sh) take priority
    over previous manifest values, which in turn take priority over defaults.
    Explicit subnets/security_groups/launch_type params always win.
    """
    root = workspace_root or get_workspace_root()
    infra = infra_output or {}
    paths = get_state_paths(extension, root)
    previous = read_json(paths.provision_manifest) or {}

    region = (
        infra.get("aws_region")
        or os.environ.get("AWS_REGION")
        or previous.get("aws_region", "us-east-1")
    )

    previous_has_ecs = bool(previous.get("ecs", {}).get("cluster"))

    if lambda_only and not previous_has_ecs:
        provision: dict[str, Any] = {
            "state_version": STATE_VERSION,
            "extension": extension,
            "updated_at": utc_now_iso(),
            "aws_region": region,
            "lambda_only": True,
        }
        _attach_lambda_block(provision, handlers_lambda, previous)
        write_json(paths.provision_manifest, provision)
        return paths.provision_manifest

    ecr_repo = infra.get("ecr_repo") or f"{extension}-handlers-ecs"
    ecr_base_uri = infra.get("ecr_base_uri") or previous.get("ecr", {}).get("repository", ecr_repo)
    # Keep existing image_uri tag if already set (deploy updates it on push)
    ecr_image_uri = previous.get("ecr", {}).get("image_uri") or f"{ecr_base_uri}:latest"

    ecs_bucket = (
        infra.get("ecs_bucket")
        or previous.get("buckets", {}).get("ecs_results_bucket")
        or f"{extension}-handlers-ecs-results"
    )
    ecs_cluster = (
        infra.get("ecs_cluster")
        or previous.get("ecs", {}).get("cluster")
        or f"{extension}-handlers"
    )
    task_definition = previous.get("ecs", {}).get("task_definition") or f"{extension}-handlers-ecs"

    effective_launch_type = launch_type or previous.get("ecs", {}).get("launch_type", "fargate")
    default_network_mode = "bridge" if effective_launch_type == "ec2" else "awsvpc"
    network_mode = previous.get("ecs", {}).get("network_mode", default_network_mode)

    # Prefer explicit params (for direct calls), then infra_output, then previous manifest
    resolved_subnets = (
        subnets if subnets is not None
        else infra.get("subnets") or previous.get("ecs", {}).get("subnets", [])
    )
    resolved_sgs = (
        security_groups if security_groups is not None
        else infra.get("security_groups") or previous.get("ecs", {}).get("security_groups", [])
    )

    provision = {
        "state_version": STATE_VERSION,
        "extension": extension,
        "updated_at": utc_now_iso(),
        "aws_region": region,
        "ecr": {
            "repository": ecr_repo,
            "image_uri": ecr_image_uri,
        },
        "ecs": {
            "cluster": ecs_cluster,
            "task_definition": task_definition,
            "launch_type": effective_launch_type,
            "network_mode": network_mode,
            "subnets": resolved_subnets,
            "security_groups": resolved_sgs,
        },
        "buckets": {
            "ecs_results_bucket": ecs_bucket,
        },
    }
    _attach_lambda_block(provision, handlers_lambda, previous)
    write_json(paths.provision_manifest, provision)
    return paths.provision_manifest


def _bootstrap_handlers_oidc_if_requested(
    extension: str,
    paths,
    parsed,
    env: dict[str, str],
    root: Path,
) -> None:
    github_repo = (parsed.github_repo or "").strip()
    if not github_repo:
        return
    from bootstrap_handlers_github_oidc import HandlersBootstrapConfig, run as bootstrap_handlers_oidc

    manifest_now = read_json(paths.provision_manifest) or {}
    region_oidc = (
        parsed.region
        or manifest_now.get("aws_region")
        or env.get("AWS_REGION")
        or "us-east-1"
    )
    ecs_bucket = (manifest_now.get("buckets") or {}).get("ecs_results_bucket")
    bootstrap_handlers_oidc(
        HandlersBootstrapConfig(
            extension=extension,
            aws_profile=parsed.profile,
            aws_region=region_oidc,
            github_repo=github_repo,
            enable_staging_role=parsed.enable_handlers_staging_role,
            ecs_results_bucket=ecs_bucket,
            apply_changes=True,
            state_out_path=paths.handlers_github_oidc,
        )
    )
    print(f"Handlers GitHub OIDC state written: {paths.handlers_github_oidc}")


def cmd_apply(extension: str, args: list[str]) -> int:
    """Create AWS infrastructure for the extension and write provision_manifest.json.

    Without --launch-type (lambda-only mode):
      1. Lambda IAM role + policy (setup_iam_role.sh)
      2. Write minimal provision_manifest.json (lambda_only: true)
         If ECS infra was already provisioned in a previous apply, those manifest
         sections are preserved and this run only refreshes the Lambda IAM.

    With --launch-type fargate|ec2 (Lambda + ECS mode):
      1. Lambda IAM role + policy (setup_iam_role.sh)
      2. ECR repo, S3 bucket, ECS cluster, task/execution roles (provision_ecs_infra.sh)
      3. EC2 ASG + capacity provider if --launch-type ec2 (provision_ecs_capacity.sh)
      4. Write full provision_manifest.json
      5. Ensure runtime_profile.json exists

    Both modes (optional):
      6. GitHub OIDC IAM roles for the handlers repo (--github-repo)
    """
    root = get_workspace_root()
    validate_extension_name(extension)
    paths = get_state_paths(extension, root)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("--launch-type", choices=["fargate", "ec2"], default=None)
    parser.add_argument("--vpc", default=None,
                        help="VPC ID to discover subnets/SG from. Uses account default VPC if omitted.")
    parser.add_argument("--region", default=None)
    parser.add_argument("--with-capacity", action="store_true",
                        help="Also provision EC2 capacity (ASG/launch template). Implied by --launch-type ec2.")
    parser.add_argument(
        "--github-repo",
        default=None,
        help="GitHub org/repo for handlers workflows (OIDC trust). When set, creates/updates IAM roles after apply.",
    )
    parser.add_argument(
        "--enable-handlers-staging-role",
        action="store_true",
        help="Create a second OIDC role for GitHub Environment 'staging' (same permissions as production).",
    )
    parsed, _ = parser.parse_known_args(args)

    env: dict[str, str] = {"EXTENSION_NAME": extension, "WORKSPACE_ROOT": str(root)}
    if parsed.profile:
        env["AWS_PROFILE"] = parsed.profile
    if parsed.region:
        env["AWS_REGION"] = parsed.region
    if parsed.vpc:
        env["ECS_VPC_ID"] = parsed.vpc

    lambda_only = parsed.launch_type is None
    region_for_manifest = (
        parsed.region or env.get("AWS_REGION") or "us-east-1"
    )
    account_id = _resolve_aws_account_id(parsed.profile, region_for_manifest)
    handlers_lambda = build_handlers_lambda_manifest_block(
        extension, region_for_manifest, account_id, root
    )

    # Step 1: Lambda IAM role (always)
    rc = _run_script("setup_iam_role.sh", env)
    if rc != 0:
        return rc

    if lambda_only:
        previous = read_json(paths.provision_manifest) or {}
        if previous.get("ecs", {}).get("cluster"):
            print(
                "[provision-infra] ECS infra already provisioned — Lambda IAM refreshed, "
                "ECS manifest preserved."
            )
            print("  To add/update ECS: re-run with --launch-type fargate|ec2")
        manifest_path = update_provision_manifest(
            extension,
            root,
            lambda_only=True,
            handlers_lambda=handlers_lambda,
        )
        print(f"Provision manifest updated: {manifest_path}")
        print(f"Run: python run.py {extension} deploy build   # Lambda zip only (no ECS cluster in manifest)")
        _bootstrap_handlers_oidc_if_requested(extension, paths, parsed, env, root)
        cmd_export_lambda_env(extension, [])
        return 0

    # Step 2: ECS base infra (ECR, S3, cluster, IAM roles)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        output_file = tf.name

    infra_env = {**env, "PROVISION_OUTPUT_FILE": output_file}
    rc = _run_script("provision_ecs_infra.sh", infra_env)
    if rc != 0:
        return rc

    infra_output: dict[str, Any] = read_json(Path(output_file)) or {}
    try:
        os.unlink(output_file)
    except OSError:
        pass

    # Step 3: EC2 capacity (if requested via flag or --launch-type ec2)
    launch_type = parsed.launch_type  # fargate or ec2 (never None here)
    needs_capacity = parsed.with_capacity or launch_type == "ec2"
    if needs_capacity:
        # Update runtime_profile so provision_ecs_capacity.sh reads ec2 as the launch type
        ensure_runtime_profile_file(extension, root)
        cmd_set_profile(extension, [f"--launch-type={launch_type}"])
        rc = _run_script("provision_ecs_capacity.sh", env)
        if rc != 0:
            return rc

    # Step 4: Write provision manifest
    # Subnets and security_groups come from infra_output (discovered by provision_ecs_infra.sh from VPC)
    region_for_manifest = str(
        infra_output.get("aws_region") or region_for_manifest
    )
    account_id = str(infra_output.get("aws_account") or account_id)
    handlers_lambda = build_handlers_lambda_manifest_block(
        extension, region_for_manifest, account_id, root
    )

    manifest_path = update_provision_manifest(
        extension,
        root,
        infra_output=infra_output,
        launch_type=launch_type,
        handlers_lambda=handlers_lambda,
    )

    # Step 5: Ensure runtime profile exists
    ensure_runtime_profile_file(extension, root)

    _bootstrap_handlers_oidc_if_requested(extension, paths, parsed, env, root)

    print(f"Provision manifest updated: {manifest_path}")
    cmd_export_lambda_env(extension, [])
    print(f"Run: python run.py {extension} deploy build")
    return 0


def cmd_destroy(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension_name(extension)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parsed, _ = parser.parse_known_args(args)
    env: dict[str, str] = {"EXTENSION_NAME": extension, "WORKSPACE_ROOT": str(root)}
    if parsed.profile:
        env["AWS_PROFILE"] = parsed.profile
    rc = _run_script("undeploy_ecs_capacity.sh", env)
    if rc != 0:
        return rc
    manifest_path = update_provision_manifest(extension, root)
    print(f"Provision manifest refreshed after destroy: {manifest_path}")
    return 0


def cmd_teardown(extension: str, args: list[str]) -> int:
    """DESTRUCTIVE: Remove ALL AWS resources created by provision-infra apply.

    Deletes (in order): EC2 capacity, ECS task definitions, ECS cluster, ECR repo,
    S3 bucket, ECS IAM roles, Lambda IAM role and managed policy, CloudWatch log groups
    (unless --keep-logs).

    Requires --yes to confirm (prevents accidental runs).
    """
    validate_extension_name(extension)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("--region", default=None)
    parser.add_argument("--yes", action="store_true", help="Confirm destructive teardown")
    parser.add_argument(
        "--keep-logs",
        action="store_true",
        help="Do not delete CloudWatch log groups (/ecs/... and Lambda if known from deploy_input)",
    )
    parsed, _ = parser.parse_known_args(args)

    if not parsed.yes:
        print("ERROR: teardown is destructive and irreversible.", file=__import__("sys").stderr)
        print("Pass --yes to confirm you want to delete all AWS resources for this extension.", file=__import__("sys").stderr)
        return 1

    root = get_workspace_root()
    paths = get_state_paths(extension, root)
    manifest = read_json(paths.provision_manifest) or {}

    env: dict[str, str] = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(root),
        "TEARDOWN_PYTHON": sys.executable,
    }
    if parsed.profile:
        env["AWS_PROFILE"] = parsed.profile
    # Pass manifest values so script uses exact names if available
    if manifest.get("aws_region") or parsed.region:
        env["AWS_REGION"] = parsed.region or manifest["aws_region"]
    if manifest.get("ecs", {}).get("cluster"):
        env["ECS_CLUSTER"] = manifest["ecs"]["cluster"]
    if manifest.get("buckets", {}).get("ecs_results_bucket"):
        env["ECS_RESULTS_BUCKET"] = manifest["buckets"]["ecs_results_bucket"]
    if parsed.keep_logs:
        env["TEARDOWN_KEEP_LOGS"] = "1"

    # Lambda log group name (before state dir is removed) — optional second CW group to delete
    lc = load_lambda_config_from_deploy_input(extension, root)
    if lc and lc.get("FunctionName"):
        env["LAMBDA_LOG_GROUP_NAME"] = f"/aws/lambda/{lc['FunctionName']}"

    from bootstrap_handlers_github_oidc import teardown_handlers_github_oidc

    region_oidc = parsed.region or manifest.get("aws_region") or env.get("AWS_REGION") or "us-east-1"
    teardown_handlers_github_oidc(extension, parsed.profile, region_oidc, apply_changes=True)
    print("Removed handlers GitHub OIDC IAM roles/policies (if present).")

    rc = _run_script("teardown_all.sh", env)
    if rc != 0:
        return rc

    # Clear local state files after successful teardown
    import shutil
    if paths.state_dir.is_dir():
        shutil.rmtree(paths.state_dir)
        print(f"Removed local state: {paths.state_dir}")
    return 0


def cmd_export(extension: str, args: list[str]) -> int:
    """Export provision manifest values as env vars for injection into launcher/vars.json.

    Writes state/<ext>/lambda_env_export.json and prints the environment block to stdout.
    """
    validate_extension_name(extension)
    rc = cmd_export_lambda_env(extension, args)
    if rc != 0:
        return rc

    paths = get_state_paths(extension)
    lambda_env = read_json(paths.lambda_env_export)
    if lambda_env:
        env_block = lambda_env.get("environment") or {}
        print()
        print("Values for launcher/vars.json (VARS section):")
        print("-" * 48)
        for key, value in env_block.items():
            print(f'    "{key}": "{value}",')
        print("-" * 48)
        print(f"Full export: {paths.lambda_env_export}")
    return 0
