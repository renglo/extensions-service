# Extension service (shared build, deploy, IAM, test, logs)

Single entry point to build, deploy, set up IAM, run locally, view logs, and test handler Lambdas for any extension that has `installer/service` with `lambda_config.json`.

## Usage (from repo root)

Use `python3` (or `python` if it points to Python 3):

```bash
# List extensions that support this (have installer/service/lambda_config.json)
python3 dev/extension-service/run.py list

# Build Lambda deployment package (Docker)
python3 dev/extension-service/run.py <extension> build

# Deploy: create or update Lambda (args passed through)
python3 dev/extension-service/run.py <extension> deploy              # first-time or update
python3 dev/extension-service/run.py <extension> deploy deploy --clean
python3 dev/extension-service/run.py <extension> deploy deploy --profile my-aws-profile
python3 dev/extension-service/run.py <extension> deploy update
python3 dev/extension-service/run.py <extension> deploy undeploy    # delete function

# Create/update IAM policy and role for the Lambda
python3 dev/extension-service/run.py <extension> setup-iam
python3 dev/extension-service/run.py <extension> setup-iam --profile my-aws-profile

# Run a handler locally (Docker)
python3 dev/extension-service/run.py <extension> run-local <handler_name>
python3 dev/extension-service/run.py <extension> run-local <handler_name> path/to/payload.json
python3 dev/extension-service/run.py <extension> run-local <handler_name> path/to/payload.json --rebuild
```

**Two build modes (use `--local` flag):**

| Goal | Command | Image | Zip |
|------|---------|--------|-----|
| **Lambda deploy** (or run-local with same arch as Lambda) | `python3 dev/extension-service/run.py exhq build` | `exhq-lambda-builder:latest` (amd64) | `lambda_deployment.zip` (amd64, for upload) |
| **Fast local runs on ARM Mac (M1/M2)** | `python3 dev/extension-service/run.py exhq build --local` | `exhq-lambda-builder:local` (arm64) | not extracted (so deploy zip stays amd64) |

- **Deploy / production:** Run `build` with no flags. That produces the amd64 image and zip; upload the zip to Lambda.
- **Local dev on ARM:** Run `build --local` once; after that, `run-local ping` automatically uses the `:local` arm64 image (no flag needed). The `:latest` amd64 image and zip are untouched.

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

## Per-extension config (unchanged)

Each extension still keeps in `extensions/<name>/installer/service/`:

- `lambda_config.json` – function name, role, runtime, etc.
- `<name>-handlers-iam-policy.json` – IAM policy document for `setup-iam`
- Optional: `example_payload.json`, `README.md`
- **`extensions/<name>/package/handlers_config.json`** – handler name → class mapping (used by `lambda_router.py`)

Script logic lives here in `dev/extension-service/scripts/` and is parameterized by `EXTENSION_NAME` and `WORKSPACE_ROOT` (set by `run.py`). Each extension’s `installer/service/` now contains only config files (no duplicate scripts).
