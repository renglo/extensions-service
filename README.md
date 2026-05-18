# Extension service (provision-infra + deploy + runtime-config)

Manages the full lifecycle of extension handler deployments across three permission stages, each with a local source of truth under `dev/extensions-service/state/<extension>/`.

Short deploy-flow diagram: [DEPLOY_FLOW.md](DEPLOY_FLOW.md).

## Stages overview

| Stage | Command | Permissions | Output |
|-------|---------|-------------|--------|
| **1 — provision-infra** | `provision-infra apply` | Admin (IAM; optional ECR/ECS/S3) | `provision_manifest.json`, optional `handlers_github_oidc.json` |
| **2a — deploy Lambda** | `deploy build` + `deploy deploy` (zip) | DevOps (Lambda create/update) | Function `{ext}-handlers` on AWS |
| **2b — deploy ECS** | `deploy build` + `deploy push` + `deploy publish` | DevOps (ECR push, ECS task def) | `release_manifest.json` |
| **3 — runtime-config** | `runtime set-profile / export-lambda-env` | Moderate (ASG update) | `runtime_profile.json`, `lambda_env_export.json` |

---

## Stage 1 — provision-infra (admin, once per environment)

Creates AWS infrastructure and writes the manifest that stages 2 and 3 consume.

**Lambda-only (default — omit `--launch-type`):**
- Lambda IAM policy + role (`setup_iam_role.sh`)
- Minimal `provision_manifest.json` with `lambda_only: true` and `lambda.LAMBDA_EXTERNAL_HANDLERS_ARN` (handlers function ARN for launcher/backend; no ECS cluster → `deploy build` produces Lambda zip only)

**Lambda + ECS (pass `--launch-type fargate` or `--launch-type ec2`):**
- Everything above, plus:
- ECR repository: `{ext}-handlers-ecs`
- S3 results bucket: `{ext}-handlers-ecs-{account_id}`
- ECS cluster: `{ext}-handlers`
- ECS task execution role + task role (IAM)
- Subnets and security group — auto-discovered from the VPC (default VPC unless `--vpc` is passed)
- EC2 ASG + launch template + capacity provider (only when `--launch-type ec2`)

Re-running **without** `--launch-type` on an environment that already has ECS in the manifest only refreshes Lambda IAM and **preserves** ECS sections (does not tear down ECS).
- **Optional:** GitHub OIDC IAM roles for the handlers repo (`--github-repo Org/repo`), using templates under `utils/github-handlers-*.template.json`. Writes `state/<ext>/handlers_github_oidc.json`. `provision-infra teardown` removes these roles/policies before deleting ECS resources.
- **Teardown:** `provision-infra teardown --yes` deletes all provisioned AWS resources. By default it also deletes CloudWatch log groups `/ecs/<ext>-handlers-ecs` and, if `state/<ext>/deploy_input.json` defines `lambda_config.FunctionName`, `/aws/lambda/<FunctionName>`. Pass **`--keep-logs`** to retain those log groups (e.g. for audits).

**Handlers IAM policy:** Always generated at apply time from the extension name and account (ECS invoke + S3 handshake). No per-extension IAM policy JSON file under `installer/service`.

```bash
# Lambda-only (default) — no ECS cluster, ECR, or S3 results bucket
python3 dev/extensions-service/run.py <extension> provision-infra apply \
  --profile my-admin-profile

# Lambda + ECS on Fargate — uses default VPC
python3 dev/extensions-service/run.py <extension> provision-infra apply \
  --profile my-admin-profile \
  --launch-type fargate

# Lambda + ECS on EC2 — also provisions ASG/capacity provider
python3 dev/extensions-service/run.py <extension> provision-infra apply \
  --profile my-admin-profile \
  --launch-type ec2

# Custom VPC (subnets/SG auto-discovered from it)
python3 dev/extensions-service/run.py <extension> provision-infra apply \
  --profile my-admin-profile \
  --vpc vpc-0abc1234

# Same as above, plus GitHub OIDC roles for CI (trusts repo:ORG/handlers-repo:environment:production)
python3 dev/extensions-service/run.py <extension> provision-infra apply \
  --profile my-admin-profile \
  --launch-type ec2 \
  --github-repo ORG/handlers-repo \
  [--enable-handlers-staging-role]

# Export values for launcher/vars.json
python3 dev/extensions-service/run.py <extension> provision-infra export
# → prints ECS_CLUSTER, ECS_TASK_DEFINITION, ECS_SUBNETS, ECS_SECURITY_GROUPS,
#          ECS_RESULTS_BUCKET, ECS_LAUNCH_TYPE, ECS_NETWORK_MODE
# → writes state/<ext>/lambda_env_export.json

# Tear down EC2 capacity only (cluster is kept, IAM roles kept)
python3 dev/extensions-service/run.py <extension> provision-infra destroy \
  --profile my-admin-profile

# DESTRUCTIVE: delete ALL AWS resources (IAM, ECR, S3, ECS cluster, roles, policy).
# By default also deletes CloudWatch log groups: /ecs/<ext>-handlers-ecs and, if
# state/<ext>/deploy_input.json has lambda_config.FunctionName, /aws/lambda/<FunctionName>.
# Also removes local state/<ext>/ directory.
python3 dev/extensions-service/run.py <extension> provision-infra teardown \
  --profile my-admin-profile --yes

# Keep CloudWatch logs for audit / debugging
python3 dev/extensions-service/run.py <extension> provision-infra teardown \
  --profile my-admin-profile --yes --keep-logs
```

---

## Stage 2 — deploy (DevOps / CI profile)

Stage 2 has **two separate paths**. Docker is used as a **build tool** in both cases; only the ECS path pushes an image to ECR.

| Target | Build | Publish to AWS | Mechanism |
|--------|-------|----------------|-----------|
| **Handlers Lambda** | `deploy build` (or `build`) | `deploy deploy` / `deploy update` | **`lambda_deployment.zip`** via `deploy_as_a_service.sh` (`create-function` / `update-function-code` with `--zip-file`) |
| **Handlers ECS** | `deploy build` (auto or `--large`) | `deploy push` + `deploy publish` | Docker image → ECR `{ext}-handlers-ecs` + task definition |


**Important:** `deploy push` does **not** deploy the Lambda. It only runs `deploy_ecs.sh` (ECR + ECS). For Lambda-only environments, stop after `deploy deploy` — do not run `deploy push`.

Requires `state/<ext>/deploy_input.json` with a `lambda_config` block (function name, role, runtime, env vars). Set `DEPLOY_INPUT_FILE` or place the file at `state/<ext>/deploy_input.json`.

### 2a — Deploy handlers Lambda (zip)

Build produces a zip; deploy uploads it to AWS Lambda.

```bash
# 1) Build zip (Docker builder image; output: extensions/<ext>/package/lambda_deployment.zip)
python3 dev/extensions-service/run.py <extension> deploy build
# shortcut:
python3 dev/extensions-service/run.py <extension> build

# 2) Create or replace the Lambda function (reads deploy_input.json → lambda_config)
python3 dev/extensions-service/run.py <extension> deploy deploy --profile my-devops-profile
python3 dev/extensions-service/run.py <extension> deploy deploy --clean --profile my-devops-profile

# 3) Update code only (function must already exist)
python3 dev/extensions-service/run.py <extension> deploy update --profile my-devops-profile

# Remove the Lambda function
python3 dev/extensions-service/run.py <extension> deploy undeploy --profile my-devops-profile
```

Subcommands `deploy`, `update`, and `undeploy` map to `deploy_as_a_service.sh`. Default **`--type lambda`** (omit `--type ecs`). The script builds the zip automatically if `lambda_deployment.zip` is missing.

**Lambda-only end-to-end** (after `provision-infra apply` without `--launch-type`):

```bash
python3 dev/extensions-service/run.py <extension> provision-infra apply --profile my-admin-profile
python3 dev/extensions-service/run.py <extension> deploy build
python3 dev/extensions-service/run.py <extension> deploy deploy --profile my-devops-profile
# Do NOT run deploy push — no ECS/ECR was provisioned for handlers.
```

### 2b — Deploy handlers ECS (ECR + task definition)

Reads ECR repo, S3 bucket, and cluster from `provision_manifest.json` (written by `provision-infra apply --launch-type fargate|ec2`).

```bash
python3 dev/extensions-service/run.py <extension> deploy build
python3 dev/extensions-service/run.py <extension> deploy push --profile my-devops-profile
python3 dev/extensions-service/run.py <extension> deploy publish --type ecs
```

Legacy-style single command (build large image + push ECS in one step):

```bash
python3 dev/extensions-service/run.py <extension> deploy deploy --type ecs --profile my-devops-profile
```

**Both Lambda and ECS** (when `handlers_config.json` lists ECS handlers):

```bash
python3 dev/extensions-service/run.py <extension> deploy build
python3 dev/extensions-service/run.py <extension> deploy deploy --type default --profile my-devops-profile
# default: zip Lambda first, then ECS deploy_ecs.sh if EXTERNAL_HANDLERS_ECS_HANDLERS is set
```

Or split explicitly:

```bash
python3 dev/extensions-service/run.py <extension> deploy build
python3 dev/extensions-service/run.py <extension> deploy deploy --profile my-devops-profile
python3 dev/extensions-service/run.py <extension> deploy push --profile my-devops-profile
python3 dev/extensions-service/run.py <extension> deploy publish --type ecs
```

### Build flags (`deploy build` / `build`)

Lambda zip is **always** built. ECS image is **optional** — built when `provision_manifest.json` includes ECS, or when forced.

```bash
# Force ECS image build before provision-infra (e.g. local image test)
python3 dev/extensions-service/run.py <extension> deploy build --large

# Skip ECS image even if manifest has ECS (Lambda zip redeploy only)
python3 dev/extensions-service/run.py <extension> deploy build --no-ecs

# ARM64 zip for run-local on Apple Silicon
python3 dev/extensions-service/run.py <extension> deploy build --local
```

| Scenario | Command | Artifacts |
|----------|---------|-----------|
| ECS provisioned (auto) | `deploy build` | Lambda zip + ECS image |
| Lambda only (no ECS in manifest) | `deploy build` | Lambda zip only |
| Force ECS before provision | `deploy build --large` | Lambda zip + ECS image |
| Skip ECS even if provisioned | `deploy build --no-ecs` | Lambda zip only |
| Local ARM (for run-local) | `deploy build --local` | Lambda zip (arm64) |

| Build mode | Docker image (build tool) | Output for deploy |
|------------|---------------------------|-------------------|
| Lambda (default) | `<ext>-lambda-builder:latest` (amd64) | `lambda_deployment.zip` → `deploy deploy` |
| ECS large | `<ext>-ecs-builder:latest` (amd64) | Image → `deploy push` / `deploy deploy --type ecs` |
| Local ARM | `<ext>-lambda-builder:local` (arm64) | zip or image for `run-local` only |

---

## Stage 3 — runtime-config (tuning, no re-provision needed)

Adjusts compute sizing (Fargate vs EC2, instance type, ASG capacity) without touching infra.
After changing profile, run `export-lambda-env` to refresh the values for `launcher/vars.json`.

```bash
# Fargate medium (default)
python3 dev/extensions-service/run.py <extension> runtime set-profile --medium

# EC2 large
python3 dev/extensions-service/run.py <extension> runtime set-profile \
  --large --launch-type ec2

# Fine-grained overrides
python3 dev/extensions-service/run.py <extension> runtime set-profile \
  --launch-type ec2 --network-mode bridge \
  --ec2-instance-type m5.2xlarge \
  --asg-min-size 1 --asg-desired-capacity 2 --asg-max-size 4

# Refresh lambda_env_export.json (same as provision-infra export)
python3 dev/extensions-service/run.py <extension> runtime export-lambda-env
```

---

## Other commands

```bash
# List extensions (directories with extensions/<name>/package/)
python3 dev/extensions-service/run.py list

# Create/update Lambda IAM role only (subset of provision-infra apply)
python3 dev/extensions-service/run.py <extension> setup-iam --profile my-admin-profile

# Provision / remove EC2 ASG capacity independently
python3 dev/extensions-service/run.py <extension> provision-ecs-capacity --profile my-admin-profile
python3 dev/extensions-service/run.py <extension> undeploy-ecs-capacity --profile my-admin-profile

# Run a handler locally via Docker
python3 dev/extensions-service/run.py <extension> run-local <handler_name>
python3 dev/extensions-service/run.py <extension> run-local <handler_name> path/to/payload.json

# View CloudWatch logs
python3 dev/extensions-service/run.py <extension> view-logs
python3 dev/extensions-service/run.py <extension> view-logs --follow
python3 dev/extensions-service/run.py <extension> view-logs --hours 24 --filter ERROR

# Invoke handler directly on AWS Lambda
python3 dev/extensions-service/run.py <extension> test <handler_name>
python3 dev/extensions-service/run.py <extension> test <handler_name> --payload-file example_payload.json
```

---

## State files (source of truth)

All under `dev/extensions-service/state/<extension>/` — gitignored except `state/schemas/`.

| File | Written by | Stage 2 role |
|------|-----------|--------------|
| `deploy_input.json` | **bootstrap merge** (copy from `bootstrap/state/<ext>/`) | **Required for stage 2.** Contains `lambda_config` (complete Lambda create payload) + `ecs_environment` (task env vars) + `ecr_image_uri`. Self-sufficient — no other file needed for a local or CI deploy. |
| `provision_manifest.json` | `provision-infra apply` | Optional. Values take priority over `deploy_input` where both exist (AWS region, ECR, ECS cluster, bucket). |
| `runtime_profile.json` | `provision-infra apply`, `runtime set-profile` | Optional. When present, used for ECS launch profile (CPU, memory, launch type). When absent, `deploy_input.ecs_environment` supplies `ECS_LAUNCH_TYPE` / `ECS_NETWORK_MODE`. |
| `handlers_github_oidc.json` | `provision-infra apply` (with `--github-repo`) | Handlers repo OIDC role ARNs — not used by stage 2 deploy scripts directly. |
| `release_manifest.json` | `deploy build/push/publish` | Tracks last build, push, and publish timestamps. |
| `lambda_env_export.json` | `provision-infra export`, `runtime export-lambda-env` | Env vars ready for `launcher/vars.json` (Stage 3 / `provision-infra export`). |

---

## Per-extension repo layout

- **`extensions/<name>/package/`** — handler code and `pyproject.toml` (required for build/run-local/list).
- **`extensions/<name>/package/handlers_config.json`** — handler routing for `lambda_router.py`.
- **`dev/extensions-service/state/<name>/`** — generated locally (gitignored except `schemas/`).

---

## Optional dependencies and `[large-dependencies]` (ECS large build)

In each extension's `package/pyproject.toml`:

- **`[project.dependencies]`** — installed for Lambda builds (kept small).
- **`[project.optional-dependencies] large-dependencies`** — heavy libs (TensorFlow, numpy, scikit-learn, hdbscan, etc.) installed only for `deploy build --large` (ECS image). Handlers that need these must list them here.

If the first `pip install` fails (e.g. missing gcc), the build retries with `--only-binary` for packages listed in **`dev/extensions-service/wheel_libs.json`** (e.g. `["hdbscan"]`). If both attempts fail, the build stops.

---

## Notes

**Handlers Lambda vs launcher backend Lambda:** This service deploys the **handlers** function (`{ext}-handlers`) as a **zip** package. The **backend** Lambda (`{env}-backend-production`) and its ECR repo are provisioned by **bootstrap → launcher**, not by `deploy push` here.

**`deploy_input.json` is the single source of truth for stage 2.** After running `python bootstrap/install.py`, copy `bootstrap/state/<ext>/deploy_input.json` to `dev/extensions-service/state/<ext>/deploy_input.json`. It contains everything `deploy deploy` and `deploy push` need — no separate `provision_manifest.json` or `runtime_profile.json` required. Set `DEPLOY_INPUT_FILE=/path/to/file` to override the default location.

**`launcher/vars.json` ECS entries** (e.g. `ECS_CLUSTER`, `ECS_SUBNETS`) come from `provision-infra export` output. Run it after first apply and after any VPC or runtime profile change.

**Windows / WSL:** if shell scripts fail with carriage return errors, run:
```bash
sed -i 's/\r$//' dev/extensions-service/scripts/*.sh
```
