from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from constants import REGLO_DEPLOYMENT_DESCRIPTION
from lib import get_workspace_root
from state_store import get_state_paths, read_json

# Keys in SECRETS that must not be injected into Lambda/ECS runtime env.
RUNTIME_ENV_EXCLUDE: frozenset[str] = frozenset(
    {
        "AWS_GITHUB_OIDC_ROLE_ARN",
    }
)

# Lambda rejects these in Environment.Variables (reserved; set by the runtime).
LAMBDA_RESERVED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
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


def validate_deploy_input_payload(payload: dict[str, Any]) -> None:
    """Require GitHub-environment envelope: GITHUB_REPOSITORY, ENVIRONMENT, VARS; SECRETS optional."""
    if not isinstance(payload.get("VARS"), dict):
        raise ValueError("deploy_input.json must contain a VARS object")
    for key in ("GITHUB_REPOSITORY", "ENVIRONMENT"):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"deploy_input.json must contain {key}")


def load_deploy_input_payload(extension: str, workspace_root: Path | None = None) -> dict[str, Any] | None:
    p = resolve_deploy_input_file(extension, workspace_root)
    if p is None:
        return None
    payload = read_json(p)
    if payload is not None:
        validate_deploy_input_payload(payload)
    return payload


def _string_vars(raw: dict[str, Any] | None) -> dict[str, str]:
    if not raw:
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if value is not None and str(value).strip() != ""
    }


def ensure_aws_region_pair(vars_map: dict[str, str]) -> None:
    """Ensure AWS_REGION and AWS_DEFAULT_REGION are both set to the same value (ECS/SDK parity)."""
    region = (vars_map.get("AWS_REGION") or vars_map.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        return
    vars_map["AWS_REGION"] = region
    vars_map["AWS_DEFAULT_REGION"] = region


def build_runtime_environment(
    payload: dict[str, Any],
    *,
    for_lambda: bool = False,
) -> dict[str, str]:
    """Merge VARS + SECRETS into a flat env map for Lambda or ECS (excludes RUNTIME_ENV_EXCLUDE).

    Lambda: drops reserved region keys (AWS sets them at runtime).
    ECS: keeps both AWS_REGION and AWS_DEFAULT_REGION (duplicated when only one is present).
    """
    validate_deploy_input_payload(payload)
    merged: dict[str, str] = {}
    merged.update(_string_vars(payload.get("VARS")))
    secrets = _string_vars(payload.get("SECRETS"))
    for key, value in secrets.items():
        if key not in RUNTIME_ENV_EXCLUDE:
            merged[key] = value
    if for_lambda:
        runtime = {
            key: value
            for key, value in merged.items()
            if key not in LAMBDA_RESERVED_ENV_KEYS
        }
        return {"PYTHONPATH": "/var/task", **runtime}
    ensure_aws_region_pair(merged)
    return merged


def _function_name_from_payload(payload: dict[str, Any], extension: str) -> str:
    validate_deploy_input_payload(payload)
    vars_block = payload.get("VARS") or {}
    for key in ("LAMBDA_HANDLERS_FUNCTION_NAME", "LAMBDA_FUNCTION_NAME"):
        name = vars_block.get(key)
        if name:
            return str(name)
    return f"{extension}-handlers"


def build_lambda_config(payload: dict[str, Any], extension: str) -> dict[str, Any]:
    """Build aws lambda create-function / update-function-configuration JSON from deploy_input."""
    validate_deploy_input_payload(payload)
    return {
        "FunctionName": _function_name_from_payload(payload, extension),
        "Role": f"{extension}-handlers-role",
        "Handler": _LAMBDA_HANDLER,
        "Runtime": _LAMBDA_RUNTIME,
        "Timeout": _LAMBDA_TIMEOUT,
        "MemorySize": _LAMBDA_MEMORY_SIZE,
        "Description": REGLO_DEPLOYMENT_DESCRIPTION,
        "Environment": {"Variables": build_runtime_environment(payload, for_lambda=True)},
    }


def get_ecr_image_uri_from_payload(payload: dict[str, Any]) -> str | None:
    validate_deploy_input_payload(payload)
    vars_block = payload.get("VARS") or {}
    uri = vars_block.get("ECR_IMAGE_URI")
    return str(uri) if uri else None


def deploy_input_from_path(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if payload is None:
        raise FileNotFoundError(path)
    validate_deploy_input_payload(payload)
    return payload


def load_lambda_config_from_deploy_input(
    extension: str, workspace_root: Path | None = None
) -> dict[str, Any] | None:
    payload = load_deploy_input_payload(extension, workspace_root)
    if not payload:
        return None
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
    try:
        cfg = build_lambda_config(payload, extension)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
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
    try:
        env = build_runtime_environment(payload, for_lambda=False)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
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
