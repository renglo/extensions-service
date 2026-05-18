from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from lib import get_workspace_root
from state_store import get_state_paths, read_json

# Keys in SECRETS (or VARS) that must not be injected into Lambda/ECS runtime env.
RUNTIME_ENV_EXCLUDE: frozenset[str] = frozenset(
    {
        "AWS_GITHUB_OIDC_ROLE_ARN",
    }
)

_LAMBDA_HANDLER = "lambda_router.lambda_handler"
_LAMBDA_RUNTIME = "python3.12"
_LAMBDA_TIMEOUT = 900
_LAMBDA_MEMORY_SIZE = 3008


def resolve_deploy_input_file(extension: str, workspace_root: Path | None = None) -> Path | None:
    """
    Return path to deploy input JSON.
    Resolution order: DEPLOY_INPUT_FILE env, then state/<ext>/deploy_input.json if it exists.
    """
    root = workspace_root or get_workspace_root()
    env_path = os.environ.get("DEPLOY_INPUT_FILE", "").strip()
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p.resolve()
        return None
    paths = get_state_paths(extension, root)
    if paths.deploy_input.is_file():
        return paths.deploy_input
    return None


def load_deploy_input_payload(extension: str, workspace_root: Path | None = None) -> dict[str, Any] | None:
    p = resolve_deploy_input_file(extension, workspace_root)
    if p is None:
        return None
    return read_json(p)


def is_github_env_shape(payload: dict[str, Any]) -> bool:
    """True when deploy_input uses VARS/SECRETS envelope (Option B)."""
    return isinstance(payload.get("VARS"), dict)


def _string_vars(raw: dict[str, Any] | None) -> dict[str, str]:
    if not raw:
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if value is not None and str(value).strip() != ""
    }


def build_runtime_environment(
    payload: dict[str, Any],
    *,
    for_lambda: bool = False,
) -> dict[str, str]:
    """
    Merge VARS + SECRETS into a flat env map for Lambda or ECS.

    Excludes RUNTIME_ENV_EXCLUDE (e.g. AWS_GITHUB_OIDC_ROLE_ARN).
    Supports legacy deploy_input (ecs_environment / lambda_config.Environment.Variables).
    """
    if is_github_env_shape(payload):
        merged: dict[str, str] = {}
        merged.update(_string_vars(payload.get("VARS")))
        secrets = _string_vars(payload.get("SECRETS"))
        for key, value in secrets.items():
            if key not in RUNTIME_ENV_EXCLUDE:
                merged[key] = value
        if for_lambda:
            return {"PYTHONPATH": "/var/task", **merged}
        return merged

    # Legacy format
    legacy_ecs = payload.get("ecs_environment")
    if isinstance(legacy_ecs, dict):
        env = _string_vars(legacy_ecs)
    else:
        lc = payload.get("lambda_config") or {}
        env_vars = (lc.get("Environment") or {}).get("Variables") or {}
        env = _string_vars(env_vars if isinstance(env_vars, dict) else {})
        if for_lambda:
            return env
        return {k: v for k, v in env.items() if k != "PYTHONPATH"}

    if for_lambda:
        return {"PYTHONPATH": "/var/task", **env}
    return env


def _function_name_from_payload(payload: dict[str, Any], extension: str) -> str:
    if is_github_env_shape(payload):
        vars_block = payload.get("VARS") or {}
        if isinstance(vars_block, dict):
            for key in ("LAMBDA_HANDLERS_FUNCTION_NAME", "LAMBDA_FUNCTION_NAME"):
                name = vars_block.get(key)
                if name:
                    return str(name)
    lc = payload.get("lambda_config")
    if isinstance(lc, dict) and lc.get("FunctionName"):
        return str(lc["FunctionName"])
    return f"{extension}-handlers"


def build_lambda_config(payload: dict[str, Any], extension: str) -> dict[str, Any]:
    """Build aws lambda create-function / update-function-configuration JSON."""
    if isinstance(payload.get("lambda_config"), dict) and not is_github_env_shape(payload):
        return dict(payload["lambda_config"])

    function_name = _function_name_from_payload(payload, extension)
    env_vars = build_runtime_environment(payload, for_lambda=True)
    return {
        "FunctionName": function_name,
        "Role": f"{extension}-handlers-role",
        "Handler": _LAMBDA_HANDLER,
        "Runtime": _LAMBDA_RUNTIME,
        "Timeout": _LAMBDA_TIMEOUT,
        "MemorySize": _LAMBDA_MEMORY_SIZE,
        "Environment": {"Variables": env_vars},
    }


def get_ecr_image_uri_from_payload(payload: dict[str, Any]) -> str | None:
    if is_github_env_shape(payload):
        vars_block = payload.get("VARS") or {}
        if isinstance(vars_block, dict):
            uri = vars_block.get("ECR_IMAGE_URI")
            if uri:
                return str(uri)
    uri = payload.get("ecr_image_uri")
    return str(uri) if uri else None


def deploy_input_from_path(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if payload is None:
        raise FileNotFoundError(path)
    if is_github_env_shape(payload):
        return payload
    if "lambda_config" in payload:
        return payload
    raise ValueError(f"{path} must contain VARS (and optional SECRETS) or legacy lambda_config")


def load_lambda_config_from_deploy_input(
    extension: str, workspace_root: Path | None = None
) -> dict[str, Any] | None:
    payload = load_deploy_input_payload(extension, workspace_root)
    if not payload:
        return None
    if isinstance(payload.get("lambda_config"), dict) and not is_github_env_shape(payload):
        cfg = payload["lambda_config"]
        return cfg if isinstance(cfg, dict) else None
    return build_lambda_config(payload, extension)


def load_environment_variables_from_deploy_input(
    extension: str, workspace_root: Path | None = None
) -> dict[str, str]:
    payload = load_deploy_input_payload(extension, workspace_root)
    if not payload:
        return {}
    return build_runtime_environment(payload, for_lambda=False)


def get_runtime_env_from_deploy_input(
    extension: str, workspace_root: Path | None = None, *, for_lambda: bool = False
) -> dict[str, str]:
    payload = load_deploy_input_payload(extension, workspace_root)
    if not payload:
        return {}
    return build_runtime_environment(payload, for_lambda=for_lambda)


def get_ecr_image_uri_from_deploy_input(
    extension: str, workspace_root: Path | None = None
) -> str | None:
    payload = load_deploy_input_payload(extension, workspace_root)
    if not payload:
        return None
    return get_ecr_image_uri_from_payload(payload)


def _cli_export_lambda_config(args: argparse.Namespace) -> int:
    payload = read_json(Path(args.path))
    if not payload:
        print(f"ERROR: could not read {args.path}", file=sys.stderr)
        return 1
    extension = args.extension.strip()
    if not extension:
        print("ERROR: --extension is required", file=sys.stderr)
        return 1
    cfg = build_lambda_config(payload, extension)
    out = Path(args.output) if args.output else Path("-")
    text = json.dumps(cfg, indent=2) + "\n"
    if str(out) == "-":
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")
    return 0


def _cli_export_runtime_env(args: argparse.Namespace) -> int:
    payload = read_json(Path(args.path))
    if not payload:
        print(f"ERROR: could not read {args.path}", file=sys.stderr)
        return 1
    env = build_runtime_environment(payload, for_lambda=False)
    out = Path(args.output) if args.output else Path("-")
    text = json.dumps(env, indent=2) + "\n"
    if str(out) == "-":
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy input helpers for extensions-service")
    sub = parser.add_subparsers(dest="command", required=True)

    p_lambda = sub.add_parser("export-lambda-config", help="Write Lambda CLI JSON from deploy_input")
    p_lambda.add_argument("path", help="Path to deploy_input.json")
    p_lambda.add_argument("--extension", required=True, help="Extension name (for FunctionName default)")
    p_lambda.add_argument("-o", "--output", help="Output file (default stdout)")
    p_lambda.set_defaults(func=_cli_export_lambda_config)

    p_env = sub.add_parser("export-runtime-env", help="Write flat runtime env JSON from deploy_input")
    p_env.add_argument("path", help="Path to deploy_input.json")
    p_env.add_argument("-o", "--output", help="Output file (default stdout)")
    p_env.set_defaults(func=_cli_export_runtime_env)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
