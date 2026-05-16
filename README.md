# Extension service (provision-infra + deploy + runtime-config)

Manages the full lifecycle of extension handler deployments across three permission stages, each with a local source of truth under `dev/extensions-service/state/<extension>/`.

Short deploy-flow diagram: [DEPLOY_FLOW.md](DEPLOY_FLOW.md).

## Stages overview

| Stage | Command | Permissions | Output |
|-------|---------|-------------|--------|
| **1 — provision-infra** | `provision-infra apply` | Admin (IAM, ECR, ECS, S3) | `provision_manifest.json`, optional `handlers_github_oidc.json` |
| **2 — deploy** | `deploy build / push / publish` | DevOps (ECR push, ECS task def) | `release_manifest.json` |
| **3 — runtime-config** | `runtime set-profile / export-lambda-env` | Moderate (ASG update) | `runtime_profile.json`, `lambda_env_export.json` |

---

## Stage 1 — provision-infra (admin, once per environment)

Creates AWS infrastructure and writes the manifest that stages 2 and 3 consume.

**Lambda-only (default — omit `--launch-type`):**
- Lambda IAM policy + role (`setup_iam_role.sh`)
- Minimal `provision_manifest.json` with `lambda_only: true` (no ECS cluster in manifest → `deploy build` produces Lambda zip only)

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

**IAM policy file:** Optional. If `extensions/<name>/installer/service/<name>-handlers-iam-policy.json` exists it is used for the Lambda handlers policy; otherwise a minimal policy is generated from the extension name and account (ECS invoke + S3 handshake).

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

Builds the Docker image and pushes it to ECR. Reads resource names from `provision_manifest.json` — no need to know bucket or cluster names manually.

```bash
# After provision-infra apply — deploy build auto-detects ECS and builds both artifacts
python3 dev/extensions-service/run.py <extension> deploy build
python3 dev/extensions-service/run.py <extension> deploy push --profile my-devops-profile
python3 dev/extensions-service/run.py <extension> deploy publish --type ecs

# Force ECS build before provision-infra has run (e.g. testing the image locally)
python3 dev/extensions-service/run.py <extension> deploy build --large

# Skip ECS build even though provision_manifest exists (Lambda-only redeploy)
python3 dev/extensions-service/run.py <extension> deploy build --no-ecs
```

**Build behavior:**

Lambda zip is **always** built. ECS image is **optional** and built automatically when `provision_manifest.json` shows ECS infra was provisioned (i.e., after `provision-infra apply`). Use `--large` to force ECS build before the manifest exists.

| Scenario | Command | Artifacts |
|----------|---------|-----------|
| ECS provisioned (auto) | `deploy build` | Lambda zip + ECS image |
| Lambda only (no ECS manifest) | `deploy build` | Lambda zip only |
| Force ECS before provision | `deploy build --large` | Lambda zip + ECS image |
| Skip ECS even if provisioned | `deploy build --no-ecs` | Lambda zip only |
| Local ARM (for run-local) | `deploy build --local` | Lambda zip (arm64) |

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

| File | Written by | Contents |
|------|-----------|----------|
| `provision_manifest.json` | `provision-infra apply` | ECR URI, S3 bucket, ECS cluster, subnets, SGs |
| `handlers_github_oidc.json` | `provision-infra apply` (with `--github-repo`) | Handlers repo OIDC role ARNs, policy names, GitHub repo string |
| `runtime_profile.json` | `provision-infra apply`, `runtime set-profile` | launch_type, network_mode, CPU/memory, ASG sizing |
| `release_manifest.json` | `deploy build/push/publish` | last build, last push, last publish timestamps |
| `lambda_env_export.json` | `provision-infra export`, `runtime export-lambda-env` | Env vars ready for `launcher/vars.json` |
| `deploy_input.json` | Manual / CI | `lambda_config` (Lambda create payload) + `ecs_environment` (task env vars) |

---

## Per-extension repo layout

- **`extensions/<name>/package/`** — handler code and `pyproject.toml` (required for build/run-local/list).
- **`extensions/<name>/installer/service/<name>-handlers-iam-policy.json`** — optional override for the Lambda handlers IAM policy; if missing, `provision-infra apply` / `setup-iam` generate a minimal policy from resource names.
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

**Deploy reads `provision_manifest.json`** for ECR repo, S3 bucket, and cluster names — no manual env var injection needed for deploy.

**`launcher/vars.json` ECS entries** (e.g. `ECS_CLUSTER`, `ECS_SUBNETS`) come from `provision-infra export` output. Run it after first apply and after any VPC or runtime profile change.

**`deploy_input.json`** is needed for Lambda deploys (`lambda_config`) and for injecting custom env vars into the ECS task (`ecs_environment`). Set `DEPLOY_INPUT_FILE=/path/to/file` or place it at `state/<ext>/deploy_input.json`.

**Windows / WSL:** if shell scripts fail with carriage return errors, run:
```bash
sed -i 's/\r$//' dev/extensions-service/scripts/*.sh
```
