# Extension service (shared build, deploy, IAM, test, logs)

Single entry point to build, deploy, set up IAM, run locally, view logs, and test handler Lambdas for any extension that has `installer/service` with `lambda_config.json`.

## Usage (from repo root)

Use `python3` (or `python` if it points to Python 3):

```bash
# List extensions that support this (have installer/service/lambda_config.json)
python3 dev/extensions-service/run.py list

# Build: Lambda (default), ECS large (--large), or local ARM (--local)
python3 dev/extensions-service/run.py <extension> build                 # Lambda zip + image
python3 dev/extensions-service/run.py <extension> build --large           # ECS image (with [large-dependencies] extra)
python3 dev/extensions-service/run.py <extension> build --local           # arm64 for run-local

# Deploy: create or update Lambda and/or ECS (--type lambda | ecs | default)
python3 dev/extensions-service/run.py <extension> deploy deploy       # Lambda only (default)
python3 dev/extensions-service/run.py <extension> deploy deploy --type ecs   # ECS only (large image)
python3 dev/extensions-service/run.py <extension> deploy deploy --type default # both if EXTERNAL_HANDLERS_ECS_HANDLERS has entries
python3 dev/extensions-service/run.py <extension> deploy deploy --clean
python3 dev/extensions-service/run.py <extension> deploy deploy --profile my-aws-profile
python3 dev/extensions-service/run.py <extension> deploy update
python3 dev/extensions-service/run.py <extension> deploy undeploy      # delete Lambda function

# Create/update IAM policy and role for the Lambda
python3 dev/extensions-service/run.py <extension> setup-iam
python3 dev/extensions-service/run.py <extension> setup-iam --profile my-aws-profile

# Run a handler locally (Docker)
python3 dev/extensions-service/run.py <extension> run-local <handler_name>
python3 dev/extensions-service/run.py <extension> run-local <handler_name> path/to/payload.json
python3 dev/extensions-service/run.py <extension> run-local <handler_name> path/to/payload.json --rebuild
```

**Build modes:**

| Goal | Command | Image | Zip |
|------|---------|--------|-----|
| **Lambda** (handlers ligeros) | `run.py <ext> build` | `<ext>-lambda-builder:latest` (amd64) | `lambda_deployment.zip` |
| **ECS large** (handlers con [large-dependencies], TensorFlow, etc.) | `run.py <ext> build --large` | `<ext>-ecs-builder:latest` (amd64) | none |
| **Local ARM (M1/M2)** | `run.py <ext> build --local` | `<ext>-lambda-builder:local` (arm64) | not extracted |

- **Deploy Lambda:** `build` then `deploy deploy` (or `deploy deploy --type lambda`). Uses zip.
- **Deploy ECS:** `build --large` then `deploy deploy --type ecs`. Creates S3 bucket (lifecycle 3 days), ECR, cluster, task definition, and writes **`extensions/<name>/installer/service/ecs_deploy_config.json`** with bucket, cluster, task_definition. Add `subnets` and `security_groups` to that file (or set `ECS_SUBNETS`, `ECS_SECURITY_GROUPS` in env) for Fargate invocations.
- **Deploy both (default):** `deploy deploy --type default` deploys Lambda if there are light handlers and ECS if there are ECS handlers (from list).

**ECS config file:** After `deploy --type ecs`, the system reads ECS settings from `ecs_deploy_config.json` when present; env vars (e.g. `ECS_SUBNETS`, `ECS_SECURITY_GROUPS`) override or fill missing keys.

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

## Per-extension config

Each extension still keeps in `extensions/<name>/installer/service/`:

- `lambda_config.json` – function name, role, runtime, etc.
- `<name>-handlers-iam-policy.json` – IAM policy document for `setup-iam`
- **`ecs_deploy_config.json`** – written by `deploy --type ecs`; the system reads it (and env) for ECS invocations. Add `subnets` and `security_groups` for Fargate.
- Optional: `example_payload.json`, `README.md`
- **`extensions/<name>/package/handlers_config.json`** – handler name → class mapping (used by `lambda_router.py`)

Script logic lives here in `dev/extensions-service/scripts/` and is parameterized by `EXTENSION_NAME` and `WORKSPACE_ROOT` (set by `run.py`). Each extension’s `installer/service/` now contains only config files (no duplicate scripts).

## Optional dependencies and [large-dependencies] (ECS large build)

In each extension's `package/pyproject.toml`: **`[project.dependencies]`** are installed for the Lambda build (kept small). **`[project.optional-dependencies]`** can define a **`large-dependencies`** extra (e.g. TensorFlow, numpy, scikit-learn); those are not installed for Lambda. The ECS large build (`build --large`) runs `pip install .[large-dependencies]`, so only that image gets the heavy libs. Handlers that need TensorFlow or other large deps must list them under `[project.optional-dependencies] large-dependencies`.
