"""
Path and config helpers for extension service.
Extensions are identified by extensions/<name>/package (handler code).
Deploy configuration lives in this package under state/<name>/deploy_input.json.
"""
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def merge_script_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for subprocess shell scripts; disables AWS CLI pager by default."""
    run_env = os.environ.copy()
    run_env.setdefault("AWS_PAGER", "")
    if extra:
        run_env.update(extra)
    return run_env


def resolve_bash() -> str | None:
    """Bash for ``.sh`` scripts. ``None`` on POSIX (run the script directly).

    On Windows, prefer Git Bash over WSL's ``System32\\bash.exe`` (WSL remaps
    ``C:\\`` to ``/mnt/c`` and breaks Docker Desktop build contexts).
    Override with ``EXTENSIONS_SERVICE_BASH``.
    """
    if os.name != "nt":
        return None
    override = os.environ.get("EXTENSIONS_SERVICE_BASH", "").strip()
    if override:
        return override
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("bash")
    if found:
        normalized = found.replace("/", "\\").lower()
        if "system32" not in normalized and "windowsapps" not in normalized:
            return found
    raise RuntimeError(
        "bash not found for running .sh scripts on Windows. "
        "Install Git for Windows or set EXTENSIONS_SERVICE_BASH to bash.exe."
    )


def run_service_script(
    script_name: str,
    env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> int:
    """Run a file under ``scripts/`` (.sh via bash on Windows; .py via sys.executable)."""
    script = get_script_dir() / script_name
    if not script.is_file():
        print(f"ERROR: Script not found: {script}", file=sys.stderr)
        return 1
    run_env = merge_script_env(env)
    cwd = get_workspace_root()
    extra = list(extra_args or [])
    if script.suffix == ".py":
        cmd = [sys.executable, str(script), *extra]
    else:
        bash = resolve_bash()
        cmd = [bash, str(script), *extra] if bash else [str(script), *extra]
    return subprocess.run(cmd, cwd=cwd, env=run_env).returncode


def docker_context_relpath(workspace_root: Path, absolute_path: Path) -> str:
    """POSIX-relative path from the Docker build context (workspace) to a file/dir.

    Used so Dockerfiles can COPY tooling staged under state/.lambda_build/ whether
    the service lives at ``ops/extensions-service`` (monorepo) or
    ``dev/extensions-service`` (CI / compose isolated tree).
    """
    root = workspace_root.resolve()
    target = absolute_path.resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"{target} is not under Docker context {root}; "
            "OUTPUT_STATE_DIR must stay inside WORKSPACE_ROOT"
        ) from exc


def get_workspace_root() -> Path:
    """Repo/workspace root (parent of top-level dirs like extensions/, dev/).

    Default is three levels above lib.py (``.../extensions-service/`` → repo root).
    ``WORKSPACE_ROOT`` overrides that so compose / CI can point the build at an
    isolated tree of unpublished packages without copying this service.
    """
    override = os.environ.get("WORKSPACE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
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


def validate_environment_name(environment: str) -> str:
    """Validate platform environment name (alias for validate_extension_name)."""
    return validate_extension_name(environment)


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


def get_env_state_dir(env: str, workspace_root: Path | None = None) -> Path:
    """Per-env state: clone ``state/<env>/``, or ``<WORKSPACE_ROOT>/dev/extensions-service/state/<env>/``."""
    from state_store import get_state_paths

    return get_state_paths(env, workspace_root).state_dir


def get_lambda_deployment_zip_path(env: str, workspace_root: Path | None = None) -> Path:
    """Lambda zip artifact path written by build and consumed by deploy."""
    from state_store import get_state_paths

    return get_state_paths(env, workspace_root).lambda_deployment_zip


def resolve_extension_repo_dir(repo: str, workspace_root: Path | None = None) -> tuple[Path, str]:
    """
    Locate handler source package/ for an extension repo folder name.
    Returns (absolute package dir, docker COPY path relative to workspace root).
    """
    root = workspace_root or get_workspace_root()
    name = (repo or "").strip()
    if not name:
        raise ValueError("Extension repo name must not be empty")
    candidates = [
        (root / name / "package", f"{name}/package"),
        (root / "extensions" / name / "package", f"extensions/{name}/package"),
    ]
    for pkg_dir, rel_copy in candidates:
        if pkg_dir.is_dir():
            return pkg_dir, rel_copy
    tried = ", ".join(str(p) for p, _ in candidates)
    raise FileNotFoundError(f"Extension repo package not found for {name!r}. Tried: {tried}")


def detect_python_package(package_dir: Path) -> str:
    """Top-level Python package folder name inside package/ (from pyproject or first subdir)."""
    pyproject = package_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            packages = (data.get("tool") or {}).get("setuptools", {}).get("packages") or []
            if packages and isinstance(packages[0], str):
                return packages[0].split(".")[0]
        except (tomllib.TOMLDecodeError, OSError, IndexError, AttributeError):
            pass
    skip_dirs = {"__pycache__", ".lambda_build"}
    for path in sorted(package_dir.iterdir()):
        if path.is_dir() and path.name not in skip_dirs and not path.name.startswith("."):
            return path.name
    raise ValueError(f"Could not detect Python package directory under {package_dir}")


def parse_extension_repo_flag(args: list[str]) -> tuple[list[str], str | None]:
    """Extract --extension-repo FOLDER from build args. Returns (remaining_args, folder or None)."""
    out: list[str] = []
    extension_repo: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--extension-repo":
            if i + 1 >= len(args):
                raise ValueError("--extension-repo requires a folder name")
            extension_repo = args[i + 1].strip()
            i += 2
            continue
        if args[i].startswith("--extension-repo="):
            extension_repo = args[i].split("=", 1)[1].strip()
            i += 1
            continue
        out.append(args[i])
        i += 1
    return out, extension_repo


def parse_extensions_flag(args: list[str]) -> tuple[list[str], list[str] | None]:
    """Extract ``--extensions a,b,c`` (primary + extras). Returns (remaining, names|None)."""
    out: list[str] = []
    names: list[str] | None = None
    i = 0
    while i < len(args):
        if args[i] == "--extensions":
            if i + 1 >= len(args):
                raise ValueError("--extensions requires a comma-separated list")
            names = [e.strip() for e in args[i + 1].split(",") if e.strip()]
            i += 2
            continue
        if args[i].startswith("--extensions="):
            names = [e.strip() for e in args[i].split("=", 1)[1].split(",") if e.strip()]
            i += 1
            continue
        out.append(args[i])
        i += 1
    return out, names


def parse_extra_extensions_flag(args: list[str]) -> tuple[list[str], list[str]]:
    """Extract --extra-extensions a,b,c from build args. Returns (remaining_args, list_of_names)."""
    out: list[str] = []
    extras: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--extra-extensions":
            if i + 1 >= len(args):
                raise ValueError("--extra-extensions requires a comma-separated list")
            extras = [e.strip() for e in args[i + 1].split(",") if e.strip()]
            i += 2
            continue
        if args[i].startswith("--extra-extensions="):
            extras = [e.strip() for e in args[i].split("=", 1)[1].split(",") if e.strip()]
            i += 1
            continue
        out.append(args[i])
        i += 1
    return out, extras


def parse_value_flag(args: list[str], flag: str) -> tuple[list[str], str | None]:
    """Extract ``--flag VALUE`` or ``--flag=VALUE``. Returns (remaining, value|None)."""
    out: list[str] = []
    value: str | None = None
    prefix = f"{flag}="
    i = 0
    while i < len(args):
        if args[i] == flag:
            if i + 1 >= len(args):
                raise ValueError(f"{flag} requires a value")
            value = args[i + 1].strip()
            i += 2
            continue
        if args[i].startswith(prefix):
            value = args[i].split("=", 1)[1].strip()
            i += 1
            continue
        out.append(args[i])
        i += 1
    return out, value or None


def parse_packages_flag(args: list[str]) -> tuple[list[str], list[str] | None]:
    """Extract ``--packages a,b,c`` (dist names). Returns (remaining, names|None)."""
    filtered, raw = parse_value_flag(args, "--packages")
    if raw is None:
        return filtered, None
    names = [p.strip() for p in raw.split(",") if p.strip()]
    if not names:
        raise ValueError("--packages requires at least one dist name")
    return filtered, names


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
