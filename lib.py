"""
Path and config helpers for extension service.
Expects to run from workspace root; paths are relative to extensions/<name>/installer/service.
"""
from pathlib import Path
import json


def get_workspace_root() -> Path:
    """Workspace root (repo root). Assumes this file lives in dev/extension-service/."""
    return Path(__file__).resolve().parent.parent.parent


def get_extensions_dir(workspace_root: Path | None = None) -> Path:
    root = workspace_root or get_workspace_root()
    return root / "extensions"


def get_script_dir() -> Path:
    """Directory containing shared shell scripts."""
    return Path(__file__).resolve().parent / "scripts"


def list_extensions(workspace_root: Path | None = None) -> list[str]:
    """
    List extension names that have installer/service with lambda_config.json
    (i.e. support build/deploy/setup-iam/run-local/view-logs/test).
    """
    root = workspace_root or get_workspace_root()
    exts = []
    ext_dir = root / "extensions"
    if not ext_dir.is_dir():
        return exts
    for path in ext_dir.iterdir():
        if path.is_dir() and not path.name.startswith("."):
            cfg = path / "installer" / "service" / "lambda_config.json"
            if cfg.is_file():
                exts.append(path.name)
    return sorted(exts)


def validate_extension(extension: str, workspace_root: Path | None = None) -> Path:
    """
    Return the installer/service directory for the extension.
    Raises FileNotFoundError if extension or lambda_config.json is missing.
    """
    root = workspace_root or get_workspace_root()
    service_dir = root / "extensions" / extension / "installer" / "service"
    if not service_dir.is_dir():
        raise FileNotFoundError(f"Extension not found or missing installer/service: {extension}")
    cfg = service_dir / "lambda_config.json"
    if not cfg.is_file():
        raise FileNotFoundError(f"Extension missing lambda_config.json: {extension}")
    return service_dir


def get_function_name(extension: str, workspace_root: Path | None = None) -> str:
    """Read Lambda function name from extension's lambda_config.json."""
    service_dir = validate_extension(extension, workspace_root)
    with open(service_dir / "lambda_config.json") as f:
        config = json.load(f)
    return config.get("FunctionName", f"{extension}-handlers")


def get_package_dir(extension: str, workspace_root: Path | None = None) -> Path:
    """Return extensions/<extension>/package directory."""
    root = workspace_root or get_workspace_root()
    return root / "extensions" / extension / "package"


def get_ecs_handlers_for_extension(extension: str, workspace_root: Path | None = None) -> list[str]:
    """
    Parse EXTERNAL_HANDLERS_ECS_HANDLERS from env or system/env_config.py.
    Format: "ext1:handler1,handler2;ext2:handler3". Returns list of handler names for this extension.
    """
    import os
    raw = os.environ.get("EXTERNAL_HANDLERS_ECS_HANDLERS", "")
    if not raw:
        root = workspace_root or get_workspace_root()
        env_config = root / "system" / "env_config.py"
        if env_config.is_file():
            try:
                with open(env_config) as f:
                    for line in f:
                        if "EXTERNAL_HANDLERS_ECS_HANDLERS" in line and "=" in line:
                            # Parse Python assignment: EXTERNAL_HANDLERS_ECS_HANDLERS = '...'
                            raw = line.split("=", 1)[1].strip().strip("'\"").strip()
                            break
            except Exception:
                pass
    result = []
    for part in raw.split(";"):
        part = part.strip()
        if ":" not in part:
            continue
        ext, handlers_str = part.split(":", 1)
        if ext.strip().lower() == extension.lower():
            result = [h.strip().lower() for h in handlers_str.split(",") if h.strip()]
            break
    return result
