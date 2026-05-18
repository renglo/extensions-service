"""
Path and config helpers for extension service.
Extensions are identified by extensions/<name>/package (handler code).
Deploy configuration lives in this package under state/<name>/deploy_input.json.
"""
import os
from pathlib import Path


def merge_script_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for subprocess shell scripts; disables AWS CLI pager by default."""
    run_env = os.environ.copy()
    run_env.setdefault("AWS_PAGER", "")
    if extra:
        run_env.update(extra)
    return run_env


def get_workspace_root() -> Path:
    """Repo/workspace root (parent of top-level dirs like extensions/, dev/). Three levels above lib.py in .../extensions-service/."""
    return Path(__file__).resolve().parent.parent.parent


def get_extensions_dir(workspace_root: Path | None = None) -> Path:
    root = workspace_root or get_workspace_root()
    return root / "extensions"


def get_script_dir() -> Path:
    """Directory containing shared shell scripts."""
    return Path(__file__).resolve().parent / "scripts"


def list_extensions(workspace_root: Path | None = None) -> list[str]:
    """
    List extension names that have a package directory under extensions/<name>/package.
    """
    root = workspace_root or get_workspace_root()
    exts = []
    ext_dir = root / "extensions"
    if not ext_dir.is_dir():
        return exts
    for path in ext_dir.iterdir():
        if path.is_dir() and not path.name.startswith("."):
            if (path / "package").is_dir():
                exts.append(path.name)
    return sorted(exts)


def validate_extension(extension: str, workspace_root: Path | None = None) -> Path:
    """
    Ensure extensions/<extension>/package exists (handler source for build/run).
    Returns the package directory path.
    """
    root = workspace_root or get_workspace_root()
    pkg = root / "extensions" / extension / "package"
    if not pkg.is_dir():
        raise FileNotFoundError(f"Extension not found or missing package directory: {extension}")
    return pkg


def validate_extension_name(extension: str) -> str:
    """
    Validate extension identifier format for infra/state-only commands.
    Does not require extensions/<name>/package.
    """
    name = (extension or "").strip()
    if not name:
        raise ValueError("Extension name must not be empty")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if any(ch not in allowed for ch in name):
        raise ValueError(
            f"Invalid extension name {extension!r}. Allowed characters: letters, numbers, '-' and '_'."
        )
    return name


def resolve_handlers_function_name(extension: str, workspace_root: Path | None = None) -> str:
    """Handlers Lambda name from deploy_input VARS or {extension}-handlers."""
    from deploy_input import load_lambda_config_from_deploy_input

    lc = load_lambda_config_from_deploy_input(extension, workspace_root)
    if lc and lc.get("FunctionName"):
        return str(lc["FunctionName"])
    return f"{extension}-handlers"


def build_handlers_lambda_arn(function_name: str, region: str, account_id: str) -> str:
    return f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"


def build_handlers_lambda_manifest_block(
    extension: str,
    region: str,
    account_id: str,
    workspace_root: Path | None = None,
) -> dict[str, str]:
    """Manifest fragment for external handlers Lambda (name + ARN env key)."""
    function_name = resolve_handlers_function_name(extension, workspace_root)
    return {
        "function_name": function_name,
        "LAMBDA_EXTERNAL_HANDLERS_ARN": build_handlers_lambda_arn(
            function_name, region, account_id
        ),
    }


def get_function_name(extension: str, workspace_root: Path | None = None) -> str:
    """Read Lambda function name from deploy_input VARS (raises if deploy_input missing)."""
    from deploy_input import load_lambda_config_from_deploy_input
    from state_store import get_state_paths

    lc = load_lambda_config_from_deploy_input(extension, workspace_root)
    if not lc:
        paths = get_state_paths(extension, workspace_root)
        raise FileNotFoundError(
            f"No deploy_input for {extension}: set DEPLOY_INPUT_FILE or create {paths.deploy_input}"
        )
    return lc.get("FunctionName", resolve_handlers_function_name(extension, workspace_root))


def get_package_dir(extension: str, workspace_root: Path | None = None) -> Path:
    """Return extensions/<extension>/package directory."""
    root = workspace_root or get_workspace_root()
    return root / "extensions" / extension / "package"


def get_extra_extensions(primary_extension: str, workspace_root: Path | None = None) -> list[str]:
    """
    Parse EXTERNAL_HANDLERS from env or system/env_config.py.
    Format: comma-separated extension names, e.g. "extension-1,extension-2".
    Returns the extra extensions to bundle alongside the primary one (primary excluded).
    Only extensions that have a package directory are included.
    """
    import os
    raw = os.environ.get("EXTERNAL_HANDLERS", "")
    if not raw:
        root = workspace_root or get_workspace_root()
        env_config = root / "system" / "env_config.py"
        if env_config.is_file():
            try:
                with open(env_config) as f:
                    for line in f:
                        stripped = line.strip()
                        # Match exactly EXTERNAL_HANDLERS (not EXTERNAL_HANDLERS_ECS_HANDLERS etc.)
                        if stripped.startswith("EXTERNAL_HANDLERS") and "=" in stripped:
                            key = stripped.split("=", 1)[0].strip()
                            if key == "EXTERNAL_HANDLERS":
                                raw = stripped.split("=", 1)[1].strip().strip("'\"").strip()
                                break
            except Exception:
                pass
    if not raw:
        return []
    root = workspace_root or get_workspace_root()
    result = []
    for e in raw.split(","):
        e = e.strip()
        if not e or e.lower() == primary_extension.lower():
            continue
        pkg_dir = root / "extensions" / e / "package"
        if not pkg_dir.is_dir():
            print(f"WARNING: EXTERNAL_HANDLERS includes '{e}' but extensions/{e}/package not found — skipping.")
            continue
        result.append(e)
    return result


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
