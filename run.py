#!/usr/bin/env python3
"""
Single entry point for extension installer/service actions.
Usage:
  python dev/extension-service/run.py <extension> <action> [action_args...]
  python dev/extension-service/run.py noma build
  python dev/extension-service/run.py exhq deploy --clean
  python dev/extension-service/run.py noma run-local my_handler
  python dev/extension-service/run.py noma view-logs --follow
  python dev/extension-service/run.py noma test my_handler
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (
    get_script_dir,
    get_workspace_root,
    get_function_name,
    get_package_dir,
    get_ecs_handlers_for_extension,
    list_extensions,
    validate_extension,
)


def _parse_profile_and_filter_args(args: list[str]) -> tuple[str | None, list[str]]:
    """Extract --profile NAME or --profile=NAME from args. Returns (profile, filtered_args)."""
    profile = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--profile":
            if i + 1 < len(args):
                profile = args[i + 1]
                i += 2
                continue
            print("ERROR: --profile requires a value", file=sys.stderr)
            sys.exit(1)
        if args[i].startswith("--profile="):
            profile = args[i].split("=", 1)[1]
            i += 1
            continue
        filtered.append(args[i])
        i += 1
    return profile, filtered


def _run_script(script_name: str, env: dict | None = None, extra_args: list[str] | None = None) -> int:
    script_dir = get_script_dir()
    script = script_dir / script_name
    if not script.is_file():
        print(f"ERROR: Script not found: {script}", file=sys.stderr)
        return 1
    run_env = {**os.environ, **(env or {})}
    cwd = get_workspace_root()
    cmd = [str(script), *(extra_args or [])]
    return subprocess.run(cmd, env=run_env, cwd=cwd).returncode


def cmd_list(_args: list[str]) -> int:
    exts = list_extensions()
    if not exts:
        print("No extensions with installer/service (lambda_config.json) found.")
        return 0
    print("Extensions with handler Lambda service:")
    for e in exts:
        print(f"  {e}")
    return 0


def cmd_build(extension: str, args: list[str]) -> int:
    validate_extension(extension)
    use_local = "--local" in args
    use_large = "--large" in args
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(get_workspace_root()),
        "EXTENSION_SERVICE_NATIVE_PLATFORM": "1" if use_local else "0",
        "EXTENSION_SERVICE_LARGE_BUILD": "1" if use_large else "0",
    }
    return _run_script("build_lambda_package.sh", env=env)


def _parse_deploy_type(args: list[str]) -> tuple[str, list[str]]:
    """Extract --type lambda|ecs|default. Returns (type, remaining_args). Default type is lambda."""
    deploy_type = "lambda"
    out = []
    i = 0
    while i < len(args):
        if args[i] in ("--type", "-t") and i + 1 < len(args):
            deploy_type = args[i + 1].lower()
            if deploy_type not in ("lambda", "ecs", "default"):
                print(f"ERROR: --type must be lambda, ecs, or default (got {deploy_type})", file=sys.stderr)
                sys.exit(1)
            i += 2
            continue
        out.append(args[i])
        i += 1
    return deploy_type, out


def cmd_deploy(extension: str, args: list[str]) -> int:
    validate_extension(extension)
    profile, rest = _parse_profile_and_filter_args(args)
    deploy_type, filtered_args = _parse_deploy_type(rest)
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(get_workspace_root()),
    }
    if profile is not None:
        env["AWS_PROFILE"] = profile

    if deploy_type == "ecs":
        return _run_script("deploy_ecs.sh", env=env)
    if deploy_type == "default":
        ecs_handlers = get_ecs_handlers_for_extension(extension)
        root = get_workspace_root()
        package_dir = get_package_dir(extension, root)
        handlers_config = package_dir / "handlers_config.json"
        all_handlers = []
        if handlers_config.is_file():
            with open(handlers_config) as f:
                data = json.load(f)
                all_handlers = list(data.get("handlers", {}).keys())
        deploy_ecs = len(ecs_handlers) > 0
        deploy_lambda = len(ecs_handlers) == 0 or (set(h.lower() for h in all_handlers) - set(ecs_handlers))
        if deploy_lambda:
            rc = _run_script("deploy_as_a_service.sh", env=env, extra_args=filtered_args)
            if rc != 0:
                return rc
        if deploy_ecs:
            return _run_script("deploy_ecs.sh", env=env)
        return 0

    return _run_script("deploy_as_a_service.sh", env=env, extra_args=filtered_args)


def cmd_setup_iam(extension: str, args: list[str]) -> int:
    validate_extension(extension)
    profile, _ = _parse_profile_and_filter_args(args)
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(get_workspace_root()),
    }
    if profile is not None:
        env["AWS_PROFILE"] = profile
    return _run_script("setup_iam_role.sh", env=env)


def cmd_run_local(extension: str, args: list[str]) -> int:
    if not args:
        print("ERROR: run-local requires a handler name", file=sys.stderr)
        print("Usage: run.py <ext> run-local <handler_name> [payload_file.json] [--rebuild]", file=sys.stderr)
        return 1
    validate_extension(extension)
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(get_workspace_root()),
    }
    return _run_script("run_handler_local.sh", env=env, extra_args=args)


def cmd_view_logs(extension: str, args: list[str]) -> int:
    validate_extension(extension)
    profile, filtered_args = _parse_profile_and_filter_args(args)
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(get_workspace_root()),
    }
    if profile is not None:
        env["AWS_PROFILE"] = profile
    return _run_script("view_lambda_logs.sh", env=env, extra_args=filtered_args)


def cmd_test(extension: str, args: list[str]) -> int:
    profile, filtered_args = _parse_profile_and_filter_args(args)
    if not filtered_args:
        print("ERROR: test requires a handler name", file=sys.stderr)
        print("Usage: run.py <ext> test <handler_name> [--payload-file file.json] [--profile PROFILE]", file=sys.stderr)
        return 1
    validate_extension(extension)
    function_name = get_function_name(extension)
    env = {
        "EXTENSION_NAME": extension,
        "WORKSPACE_ROOT": str(get_workspace_root()),
        "LAMBDA_FUNCTION_NAME": function_name,
    }
    if profile is not None:
        env["AWS_PROFILE"] = profile
    return _run_script("test_lambda_handler.py", env=env, extra_args=filtered_args)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run.py <extension> <action> [action_args...]", file=sys.stderr)
        print("       run.py list", file=sys.stderr)
        print("", file=sys.stderr)
        print("Actions: build, deploy, setup-iam, run-local, view-logs, test", file=sys.stderr)
        print("  build       Build package (Docker). --local = arm64 for run-local; --large = ECS image with [ml] deps; omit = Lambda zip.", file=sys.stderr)
        print("  deploy      deploy | update | undeploy  (e.g. deploy deploy --clean). --type lambda|ecs|default (default = lambda)", file=sys.stderr)
        print("  setup-iam   Create/update IAM policy and role for the Lambda (--profile NAME)", file=sys.stderr)
        print("  run-local   Run a handler locally (Docker). Uses :local image if present (from build --local), else :latest. Args: <handler_name> [payload.json] [--rebuild]", file=sys.stderr)
        print("  view-logs   Tail CloudWatch logs. Optional: --follow, --filter PATTERN, --hours N, --profile NAME", file=sys.stderr)
        print("  test        Invoke handler on AWS Lambda. Args: <handler_name> [--payload-file file.json] [--profile NAME]", file=sys.stderr)
        return 1

    if sys.argv[1].lower() == "list":
        return cmd_list(sys.argv[2:])

    if len(sys.argv) < 3:
        print("Usage: run.py <extension> <action> [action_args...]", file=sys.stderr)
        return 1

    extension = sys.argv[1]
    action = sys.argv[2].lower().replace("_", "-")
    rest = sys.argv[3:]

    handlers = {
        "build": cmd_build,
        "deploy": cmd_deploy,
        "setup-iam": cmd_setup_iam,
        "run-local": cmd_run_local,
        "view-logs": cmd_view_logs,
        "test": cmd_test,
    }
    if action not in handlers:
        print(f"Unknown action: {action}", file=sys.stderr)
        print("Actions: build, deploy, setup-iam, run-local, view-logs, test", file=sys.stderr)
        return 1

    return handlers[action](extension, rest)


if __name__ == "__main__":
    sys.exit(main())
