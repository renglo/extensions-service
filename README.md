# Extension service (provision-infra + deploy + runtime-config)

Operational split for extension handlers with local source of truth in `dev/extensions-service/state/<extension>/`.

Short deploy-flow diagram: [DEPLOY_FLOW.md](DEPLOY_FLOW.md).

- `provision-infra`: provisioning baseline (IAM and optional ECS EC2 capacity), writes `provision_manifest.json`
- `deploy`: build/push/publish lifecycle, writes `release_manifest.json`
- `runtime-config`: runtime profile and backend Lambda env export, writes `runtime_profile.json` and `lambda_env_export.json`

## Usage (from repo root)

Use `python3` (or `python` if it points to Python 3):

```bash
# List extensions (have extensions/<name>/package)
python3 dev/extensions-service/run.py list

# SysAdmin profile — requires extensions/<name>/installer/service/<name>-handlers-iam-policy.json
python3 dev/extensions-service/run.py <extension> provision-infra apply --profile my-admin-profile
python3 dev/extensions-service/run.py <extension> provision-infra apply --with-capacity
python3 dev/extensions-service/run.py <extension> provision-infra destroy

# Release (DevOps/OIDC profile)
python3 dev/extensions-service/run.py <extension> deploy build
python3 dev/extensions-service/run.py <extension> deploy build --large
python3 dev/extensions-service/run.py <extension> deploy build --local
python3 dev/extensions-service/run.py <extension> deploy push --profile my-devops-profile
python3 dev/extensions-service/run.py <extension> deploy publish --type ecs

# Runtime-config
python3 dev/extensions-service/run.py <extension> runtime set-profile --medium
python3 dev/extensions-service/run.py <extension> runtime set-profile --launch-type ec2 --network-mode bridge
python3 dev/extensions-service/run.py <extension> runtime export-lambda-env

# Wrappers compatible with older docs
python3 dev/extensions-service/run.py <extension> build
python3 dev/extensions-service/run.py <extension> deploy deploy --type ecs
python3 dev/extensions-service/run.py <extension> ecs-profile --medium

# Run a handler locally (Docker)
python3 dev/extensions-service/run.py <extension> run-local <handler_name>
python3 dev/extensions-service/run.py <extension> run-local <handler_name> path/to/payload.json
python3 dev/extensions-service/run.py <extension> run-local <handler_name> path/to/payload.json --rebuild
```
**State JSONs (source of truth):**

- `dev/extensions-service/state/<extension>/provision_manifest.json`
- `dev/extensions-service/state/<extension>/runtime_profile.json`
- `dev/extensions-service/state/<extension>/release_manifest.json`
- `dev/extensions-service/state/<extension>/lambda_env_export.json`
- `dev/extensions-service/state/<extension>/deploy_input.json` (deploy input contract: `lambda_config` + `ecs_environment`)

Create `deploy_input.json` under `state/<ext>/` (or set **`DEPLOY_INPUT_FILE`**): it must contain `lambda_config` (Lambda create/update payload shape) and optional `ecs_environment` (ECS task plain env vars). Paths under `state/<ext>/` are gitignored except `state/schemas/`; commit schemas, not runtime state.

- `DEPLOY_INPUT_FILE=/path/to/deploy_input.json` for any `deploy` / Lambda operations
- Optional shell overrides: `LAMBDA_CONFIG_FILE` (Lambda CLI JSON), `ECS_ENV_FILE` (JSON env map), `ECS_PROFILE_FILE`
- `deploy_as_a_service.sh` / `deploy_ecs.sh`: require `DEPLOY_INPUT_FILE` unless you explicitly set `LAMBDA_CONFIG_FILE` or `ECS_ENV_FILE`; there is **no** default under `installer/service/` anymore.

**Build modes (deploy build):**

| Goal | Command | Image | Zip |
|------|---------|--------|-----|
| **Lambda** | `run.py <ext> deploy build` | `<ext>-lambda-builder:latest` (amd64) | `lambda_deployment.zip` |
| **ECS large** | `run.py <ext> deploy build --large` | `<ext>-ecs-builder:latest` (amd64) | none |
| **Local ARM** | `run.py <ext> deploy build --local` | `<ext>-lambda-builder:local` (arm64) | not extracted |

- **Provision baseline:** `provision-infra apply` (admin profile).
- **Deploy ECS:** `deploy build --large`, `deploy push`, `deploy publish --type ecs`.
- **Runtime profile:** `runtime set-profile ...` and `runtime export-lambda-env` to materialize backend env values.

- **Deploy both (default):** `deploy deploy --type default` deploys Lambda if there are light handlers and ECS if there are ECS handlers (from list).

  ```bash
  python3 dev/extension-service/run.py exhq build --local
  python3 dev/extension-service/run.py exhq run-local ping
  ```

```bash
# View CloudWatch logs
python3 dev/extension-service/run.py <extension> view-logs
python3 dev/extension-service/run.py <extension> view-logs --follow
python3 dev/extension-service/run.py <extension> view-logs --hours 24 --filter ERROR

# Invoke handler on AWS Lambda
python3 dev/extension-service/run.py <extension> test <handler_name>
python3 dev/extension-service/run.py <extension> test <handler_name> --payload-file example_payload.json
```

## Examples

```bash
python3 dev/extension-service/run.py noma build
python3 dev/extension-service/run.py noma deploy deploy
python3 dev/extension-service/run.py exhq setup-iam
python3 dev/extension-service/run.py noma run-local my_handler example_payload.json
python3 dev/extension-service/run.py noma view-logs --follow
python3 dev/extension-service/run.py noma test my_handler
```

## Per-extension repo layout

- **`extensions/<name>/package/`** – handler code and `pyproject.toml` (required for `list`, build, run-local).
- **`extensions/<name>/installer/service/<name>-handlers-iam-policy.json`** – IAM policy JSON for `setup-iam` / `provision-infra apply` (required for those commands).
- **`dev/extensions-service/state/<name>/`** – generated locally (ignored by git except `schemas/`): `deploy_input.json`, `provision_manifest.json`, `runtime_profile.json`, etc.

ECS sizing and network (`launch_type`, `network_mode`, CPU/memory): use `run.py <name> runtime set-profile ...` → writes **`runtime_profile.json`**. Fallback: deprecated `installer/ecs_profile.json` is still read **only if** state `runtime_profile.json` does not exist and `ECS_PROFILE_FILE` is unset (`ecs_profile.py export-for-deploy`).

ECS task env vars: **`deploy_input.json`** → `ecs_environment`, or set **`ECS_ENV_FILE`** to a JSON object path (explicit only).

Lambda deploy reads **`DEPLOY_INPUT_FILE`** and extracts **`lambda_config`**, or set **`LAMBDA_CONFIG_FILE`** to that Lambda CLI payload JSON directly.

Deploy still writes **`extensions/<name>/installer/service/ecs_deploy_config.json`** when you run ECS deploy scripts (for backward compatibility). Prefer **state** + `DEPLOY_INPUT_FILE` in CI.

- **`extensions/<name>/package/handlers_config.json`** – handler routing for `lambda_router.py`

## Optional dependencies and [large-dependencies] (ECS large build)

In each extension's `package/pyproject.toml`: **`[project.dependencies]`** are installed for the Lambda build (kept small). **`[project.optional-dependencies]`** can define a **`large-dependencies`** extra (e.g. TensorFlow, numpy, scikit-learn); those are not installed for Lambda. The ECS large build (`build --large`) runs `pip install .[large-dependencies]`, so only that image gets the heavy libs. Handlers that need TensorFlow or other large deps must list them under `[project.optional-dependencies] large-dependencies`. If the first install fails (e.g. a package needs to compile and gcc is missing), the build retries with `PIP_ONLY_BINARY` for packages listed in **`dev/extensions-service/wheel_libs.json`** (package names only, e.g. `["hdbscan"]`); if both attempts fail, the build stops.

**Note**: for windows, running on WSL:
```bash
 sed -i 's/\r$//' dev/extensions-service/scripts/*.sh
```
might be necessary before build and deploy