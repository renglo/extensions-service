from __future__ import annotations

import argparse
import sys

from deploy_input import (
    get_ecr_image_uri_from_deploy_input,
    get_runtime_env_from_deploy_input,
    resolve_deploy_input_file,
)
from lib import (
    get_env_state_dir,
    get_lambda_deployment_zip_path,
    get_workspace_root,
    parse_extension_repo_flag,
    parse_extensions_flag,
    parse_extra_extensions_flag,
    parse_packages_flag,
    parse_value_flag,
    run_service_script,
    validate_extension_name,
)
from state_store import STATE_VERSION, default_release_manifest, ensure_state_dir, get_state_paths, read_json, utc_now_iso, write_json


def _run_script(script_name: str, env: dict[str, str], extra_args: list[str] | None = None) -> int:
    return run_service_script(script_name, env=env, extra_args=extra_args)


def _load_release_manifest(extension: str):
    paths = get_state_paths(extension)
    data = read_json(paths.release_manifest) or default_release_manifest(extension)
    return paths, data


def _build_env_for_single(
    env_name: str,
    root,
    *,
    local: bool,
    large: bool,
    wheelhouse: str,
    assets: str,
    packages: list[str],
) -> dict[str, str]:
    validate_extension_name(env_name)
    state_dir = get_env_state_dir(env_name, root)
    ensure_state_dir(get_state_paths(env_name, root))
    deployment_zip = get_lambda_deployment_zip_path(env_name, root)
    kind = "ecs" if large else "lambda"
    print(f"Build kind:  {kind}")
    if not large:
        print(f"Output zip: {deployment_zip}")
    print(f"Wheelhouse: {wheelhouse}")
    print(f"Assets:     {assets}")
    print(f"Packages:   {', '.join(packages)}")
    return {
        "EXTENSION_NAME": env_name,
        "EXTENSION_REPO": env_name,
        "DOCKER_SOURCE_COPY_PATH": "",
        "PYTHON_PACKAGE": "",
        "OUTPUT_STATE_DIR": str(state_dir),
        "DEPLOYMENT_ZIP": str(deployment_zip),
        "WORKSPACE_ROOT": str(root),
        "EXTENSION_SERVICE_NATIVE_PLATFORM": "1" if local else "0",
        "EXTENSION_SERVICE_LARGE_BUILD": "1" if large else "0",
        "HANDLERS_WHEELHOUSE": str(wheelhouse),
        "HANDLERS_ASSETS": str(assets),
        "HANDLERS_PACKAGES": ",".join(packages),
    }


def _build_single(
    extension: str,
    root,
    local: bool,
    *,
    large: bool,
    wheelhouse: str,
    assets: str,
    packages: list[str],
) -> int:
    """Run one build pass and update the release manifest. Returns the script exit code."""
    from pathlib import Path
    import shutil

    env = _build_env_for_single(
        extension,
        root,
        local=local,
        large=large,
        wheelhouse=wheelhouse,
        assets=assets,
        packages=packages,
    )
    rc = _run_script("build_lambda_package.sh", env=env)
    if rc != 0:
        return rc
    paths_for_copy = get_state_paths(extension, root)
    handlers_cfg_dst = paths_for_copy.state_dir / "handlers_config.json"
    handlers_cfg_src = Path(assets) / "handlers_config.json"
    if handlers_cfg_src.is_file():
        shutil.copy2(handlers_cfg_src, handlers_cfg_dst)
    paths, manifest = _load_release_manifest(extension)
    mode = "ecs" if large else "lambda"
    image_kind = "ecs-builder" if large else "lambda-builder"
    image = f"{extension}-{image_kind}:{'local' if local else 'latest'}"
    manifest["state_version"] = STATE_VERSION
    manifest["updated_at"] = utc_now_iso()
    manifest.setdefault("builds", {})
    manifest["builds"][mode] = {
        "image": image,
        "platform": "linux/arm64" if local else "linux/amd64",
        "created_at": utc_now_iso(),
    }
    last = {
        **manifest["builds"][mode],
        "mode": mode,
    }
    if not large:
        last["lambda_deployment_zip"] = str(get_lambda_deployment_zip_path(extension, root))
    manifest["last_build"] = last
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
    """Build Lambda and/or ECS images from a prepare_handlers_wheelhouse output.

    Flags:
      --wheelhouse DIR    find-links dir from prepare_handlers_wheelhouse.py
      --assets DIR        handlers-assets/ (router + configs)
      --packages a,b      ordered pip dist names (e.g. arbitium-lab,arbitium-triage)
      --local             Build as ARM64 `:local` (arch/tag only)
      --no-ecs            Skip ECS/large image even if provisioned
      --large             Also build *-ecs-builder with [large-dependencies]
                          (prepare with --with-large-deps first)
    """
    from pathlib import Path

    root = get_workspace_root().resolve()
    try:
        filtered_args, extensions_list = parse_extensions_flag(args)
        filtered_args, extension_repo = parse_extension_repo_flag(filtered_args)
        filtered_args, extra_extensions = parse_extra_extensions_flag(filtered_args)
        filtered_args, wheelhouse = parse_value_flag(filtered_args, "--wheelhouse")
        filtered_args, assets = parse_value_flag(filtered_args, "--assets")
        filtered_args, packages = parse_packages_flag(filtered_args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if extensions_list or extension_repo or extra_extensions:
        print(
            "ERROR: --extensions/--extension-repo/--extra-extensions are removed; "
            "use --wheelhouse/--assets/--packages (dist names).",
            file=sys.stderr,
        )
        return 1

    try:
        build_local = "--local" in filtered_args
        skip_ecs = "--no-ecs" in filtered_args
        force_large = "--large" in filtered_args

        wh_path = Path(wheelhouse).resolve() if wheelhouse else None
        assets_path = Path(assets).resolve() if assets else None
        has_wheelhouse = bool(wh_path and assets_path and packages)
        if any([wheelhouse, assets, packages]) and not has_wheelhouse:
            print(
                "ERROR: build requires --wheelhouse, --assets, and --packages together",
                file=sys.stderr,
            )
            return 1
        if not has_wheelhouse:
            print(
                "ERROR: build requires --wheelhouse/--assets/--packages "
                "(see prepare_handlers_wheelhouse.py).",
                file=sys.stderr,
            )
            return 1
        if wh_path is not None and not wh_path.is_dir():
            print(f"ERROR: --wheelhouse not a directory: {wh_path}", file=sys.stderr)
            return 1
        if assets_path is not None and not assets_path.is_dir():
            print(f"ERROR: --assets not a directory: {assets_path}", file=sys.stderr)
            return 1
        if force_large and skip_ecs:
            print(
                "ERROR: --large and --no-ecs conflict; omit --no-ecs to build ECS image",
                file=sys.stderr,
            )
            return 1

        print("==> Building Lambda zip / lambda-builder image...")
        rc = _build_single(
            extension,
            root,
            local=build_local,
            large=False,
            wheelhouse=str(wh_path),
            assets=str(assets_path),
            packages=packages,
        )
        if rc != 0:
            return rc

        want_large = force_large or (not skip_ecs and _ecs_is_provisioned(extension))
        if want_large:
            print("==> Building ECS ecs-builder image ([large-dependencies])...")
            rc = _build_single(
                extension,
                root,
                local=build_local,
                large=True,
                wheelhouse=str(wh_path),
                assets=str(assets_path),
                packages=packages,
            )
            if rc != 0:
                return rc

        return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_push(extension: str, args: list[str]) -> int:
    root = get_workspace_root()
    validate_extension_name(extension)
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

    # ECS launch profile: resolved in deploy_ecs.sh (manifest → deploy_input → AWS CLI).
    # runtime_profile.json is stage 3 only; not used here.

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

