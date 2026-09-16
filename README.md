# Extension service (provision-infra + deploy + runtime-config)

Manages the full lifecycle of extension handler deployments across three permission stages, each with a local source of truth under `dev/extensions-service/state/<env>/` (or next to this package under `ops/extensions-service/state/<env>/` in the monorepo).

**Handlers packaging (1 image, N dists):** see [HANDLERS.md](HANDLERS.md) — prepare wheelhouse then `run.py build --wheelhouse/--assets/--packages`. Deploy happy path is **BOM Actions + SSM**.

Short deploy-flow diagram: [DEPLOY_FLOW.md].

## Stages overview

| Stage | Command | Permissions | Output |
|-------|---------|-------------|--------|
| 1 — provision-infra | `provision-infra apply` | Admin (IAM; optional ECR/ECS/S3) | `provision_manifest.json`, optional `handlers_github_oidc.json` |
| 2a — deploy Lambda | `deploy build` + `deploy deploy` (zip) | DevOps (Lambda create/update) | Function `{ext}-handlers` on AWS |
| 2b — deploy ECS | `deploy build` + `deploy push` | DevOps (ECR push, ECS task def) | ECR image + task definition |
| 3 — runtime-config | `runtime set-profile / export-lambda-env` | Moderate (ASG update) | `runtime_profile.json`, `lambda_env_export.json` |

---

## Quick setup guide

Run from the repo root. Replace `<env>` and `<aws-profile>` with your values.

**`<env>`** is the platform environment name (same as `bootstrap/install.py` and launcher). It selects `state/<env>/` and AWS resource prefixes from provision.

**Provision stage:** Is recomendend to be excecuted from the main infra installer, using `bootstrap/install.py`; but it can be done in the main proyect folder (where you have `system/`). But you will not have a pre-made `deploy_input.json`. In both cases, the json must be manally copied into `extensions-service/state/<env>`

**Before stage 2:** place `deploy_input.json` at `dev/extensions-service/state/<env>/deploy_input.json` (see [Stage 2 — deploy]). After bootstrap, copy it from `bootstrap/state/<env>/deploy_input.json`.

**github-repo:** Using this option will create an OIDC configuration for the specified GitHub repository. This can later be used to grant permission to a workflow.yml file in that repository to perform the Build and Deploy phases.

### Lambda only

Provisions handlers IAM + Lambda path only (no ECS cluster, ECR, or S3 results bucket).

1. **Provision (admin, once)**

  ```bash
  python3 dev/extensions-service/run.py <env> provision-infra apply \
    --profile <aws-profile>
    --github-repo <org>/<handlers-repo> #Optional
  ```

2. **Build** the Lambda zip (written to `extensions-service/state`)

  ```bash
  # Prepare wheelhouse (monorepo DX) — see HANDLERS.md
  python3 ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
    --from-monorepo extensions/arbitium/package,extensions/arbitiumtriage/package \
    --out .handlers-build
  python3 ops/extensions-service/run.py <env> build --no-ecs \
    --wheelhouse .handlers-build/wheelhouse \
    --assets .handlers-build/handlers-assets \
    --packages arbitium-lab,arbitium-triage
  # Local ARM image for run-local (Linux): add --local (arch/tag only)
  ```

  Handlers packaging details: [HANDLERS.md](HANDLERS.md).

  `WORKSPACE_ROOT` overrides the repo root (default: parent of `dev/` or `ops/`).
  `renglo-ci compose handlers-build` sets it to an isolated tree of unpublished
  packages so this service can build without copying itself into that tree.
  Build state (Dockerfile, zip, `.lambda_build`) then lands under
  `$WORKSPACE_ROOT/dev/extensions-service/state/<env>/` so `docker build -f`
  and the context are the same tree. Deploy/provision on the live clone still
  use `dev/extensions-service/state/<env>/`.

  3. **Deploy** the zip to AWS Lambda

  ```bash
  python3 dev/extensions-service/run.py <env> deploy deploy \
    --profile <aws-profile>
  ```

### ECS + Lambda

Provisions handlers IAM plus ECS cluster, ECR, and S3 results bucket. Uses the default VPC for subnets/SG unless you pass `--vpc` (stage 1 options).

1. **Provision (admin, once)**

  ```bash
  python3 dev/extensions-service/run.py <env> provision-infra apply \
    --profile <aws-profile> \
    --launch-type <ec2|fargate>
    --github-repo <org>/<handlers-repo> #Optional
  ```

2. **Build** the Lambda zip (and optionally ECS image) from a prepared wheelhouse (see [HANDLERS.md](HANDLERS.md))

  ```bash
  python3 ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
    --from-monorepo extensions/arbitium/package,extensions/arbitiumtriage/package \
    --out .handlers-build
  python3 ops/extensions-service/run.py <env> build --no-ecs \
    --wheelhouse .handlers-build/wheelhouse \
    --assets .handlers-build/handlers-assets \
    --packages arbitium-lab,arbitium-triage

  # ECS / large (add --with-large-deps on prepare, then --large on build):
  python3 ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
    --from-monorepo extensions/arbitium/package,extensions/arbitiumtriage/package \
    --with-large-deps --out .handlers-build
  python3 ops/extensions-service/run.py <env> build --large \
    --wheelhouse .handlers-build/wheelhouse \
    --assets .handlers-build/handlers-assets \
    --packages arbitium-lab,arbitium-triage
  ```

3. **Deploy** Lambda (zip) and push the ECS image (ECR + task definition)

  ```bash
  python3 dev/extensions-service/run.py <env> deploy deploy \
    --profile <aws-profile>

  python3 dev/extensions-service/run.py <env> deploy push \
    --profile <aws-profile>
  ```

---

## Stage 1 — provision-infra (admin, once per environment)

Creates AWS infrastructure and writes manifests that later stages consume.

**Lambda-only (default — omit `--launch-type`):** Lambda IAM policy + role; `provision_manifest.json` with handlers Lambda ARN. No ECS → stage 2 builds a zip only.

**Lambda + ECS (`--launch-type ec2` or `fargate`):** ECR `{ext}-handlers-ecs`, S3 results bucket, ECS cluster, task roles, subnets/SG (default VPC or `--vpc`). EC2 also gets ASG + capacity provider.

Re-running without `--launch-type` on an environment that already has ECS refreshes Lambda IAM and preserves ECS sections (does not tear down ECS).

**Handlers IAM policy:** Always generated at apply time (ECS invoke + S3 handshake). 

```bash
python3 dev/extensions-service/run.py <env> provision-infra apply \
  --profile <aws-profile>
```

Optional:

| Flag / subcommand | When to use |
|-------------------|-------------|
| `--launch-type ec2` | ECS on EC2 (ASG + capacity provider) |
| `--launch-type fargate` | ECS on Fargate |
| `--vpc vpc-...` | Non-default VPC (subnets/SG discovered from it) |
| `--github-repo Org/handlers-repo` | GitHub OIDC for handlers CI → `handlers_github_oidc.json` |
| `--enable-handlers-staging-role` | Second OIDC role for GitHub Environment `staging` |

**Other commands (optional):**

Export env vars for `launcher/vars.json` and write `state/<env>/lambda_env_export.json`:

```bash
python3 dev/extensions-service/run.py <env> provision-infra export
```

Tear down EC2 capacity only (cluster and IAM kept):

```bash
python3 dev/extensions-service/run.py <env> provision-infra destroy \
  --profile <aws-profile>
```

Delete all provisioned AWS resources and local `state/<env>/`. Use flag `--keep-logs` to Keep CloudWatch log groups when tearing down:

```bash
python3 dev/extensions-service/run.py <env> provision-infra teardown \
  --profile <aws-profile> --yes --keep-logs
```

---

## Stage 2 — deploy (DevOps / CI profile)

Stage 2 has two publish paths: Lambda (zip) and ECS (Docker image). Docker is a build tool in both cases; only the ECS path pushes to ECR.

| Target | Build | Publish to AWS |
|--------|-------|----------------|
| Handlers Lambda | `build --wheelhouse/...` (always) | `deploy deploy` / `deploy update` |
| Handlers ECS | `build --large` (or auto if ECS provisioned) | `deploy push` |

`deploy push` does not deploy Lambda. For Lambda-only environments, stop after `deploy deploy`.

Stage 2 needs `dev/extensions-service/state/<env>/deploy_input.json`. Can be generated by `bootstrap/install.py`, and must have shape as defined in `extensions-service\state\schemas\deploy_input.schema.json`

Optional: `provision_manifest.json` in state supplements deploy push (cluster, launch type). `runtime_profile.json` is for stage 3 (`runtime set-profile`), not deploy push.

### `build` / `deploy build`

`build` always produces the Lambda zip / `*-lambda-builder` from a **prepared wheelhouse** (`--wheelhouse`, `--assets`, `--packages`). Pass `--large` (or leave ECS auto-detect on) for `*-ecs-builder` with `[large-dependencies]` — prepare with `--with-large-deps` first. `--no-ecs` skips the ECS image.

```bash
# See HANDLERS.md — prepare wheelhouse first, then:
python3 ops/extensions-service/run.py <env> build --no-ecs \
  --wheelhouse .handlers-build/wheelhouse \
  --assets .handlers-build/handlers-assets \
  --packages arbitium-lab,arbitium-triage

python3 ops/extensions-service/run.py <env> build --large \
  --wheelhouse .handlers-build/wheelhouse \
  --assets .handlers-build/handlers-assets \
  --packages arbitium-lab,arbitium-triage
```

**Flags:**

| Flag | When to use |
|------|-------------|
| `--wheelhouse` / `--assets` / `--packages` | **Required.** From `prepare_handlers_wheelhouse.py`; `--packages` are dist names. |
| `--large` | Also build `*-ecs-builder` with `[large-dependencies]` |
| `--no-ecs` | Skip ECS image even if provisioned |
| `--local` | Lambda/ECS platform/tag arm64 `:local` |

| Build output | Used by |
|--------------|---------|
| `extensions-service/state/<env>/lambda_deployment.zip` | `deploy deploy` / `deploy update` |
| `<env>-lambda-builder:latest` | Local DEV_DOCKER sync (Lambda) |
| `<env>-ecs-builder:latest` | `deploy push` / ECS async; local sync smoke for ECS handlers |

### `deploy deploy` / `deploy update` / `deploy undeploy`

Uses `deploy_input.json` and `state/<env>/lambda_deployment.zip`. Subcommands map to `deploy_as_a_service.sh` (creates/updates the `{ext}-handlers` function).

```bash
python3 dev/extensions-service/run.py <env> deploy deploy \
  --profile <aws-profile>
```

**Optional:**

| Flag / subcommand | When to use |
|-------------------|-------------|
| `deploy update` | Update code/config on an existing function (preferred in CI) |
| `deploy deploy --clean` | Delete and recreate the function |
| `deploy undeploy` | Remove the Lambda function |
| `--type ecs` | Legacy: build large image + run ECS push in one step (prefer `build --large` + `deploy push`) |
| `--type default` | Zip Lambda, then ECS if extension config lists ECS handlers |

**Large Lambda packages (>50 MB zipped):** `deploy_as_a_service.sh` uploads to `VARS.S3_BUCKET_NAME` from `deploy_input.json` and calls `create-function` / `update-function-code` with `--s3-bucket` / `--s3-key`. The GitHub handlers OIDC role needs `s3:PutObject` on that bucket (in addition to the ECS results bucket). Override with `LAMBDA_DEPLOY_S3_BUCKET`; force S3 with `LAMBDA_FORCE_S3_UPLOAD=1`.

### `deploy push`

ECS only. Pushes the Docker image to ECR and registers/updates the task definition (`deploy_ecs.sh`). Reads cluster, ECR, bucket, and ECS launch/network mode from `provision_manifest.json` and/or `deploy_input.json` `VARS` (then AWS CLI if needed). Does not use `runtime_profile.json` (stage 3).

```bash
python3 dev/extensions-service/run.py <env> deploy push \
  --profile <aws-profile>
```

### `deploy publish` (optional)

Record a completed ECS release in `release_manifest.json` (no AWS changes; optional bookkeeping for CI or audit):

```bash
python3 dev/extensions-service/run.py <env> deploy publish --type ecs
```

`--type` is only a label stored as `last_publish.target` (default `ecs`). It does not choose Fargate vs EC2. 

---

## Stage 3 — runtime-config (tuning, no re-provision)

Adjust compute sizing (Fargate vs EC2, instance type, ASG) without reprovisioning infra. After profile changes, run `export-lambda-env` to refresh values for `launcher/vars.json`.

```bash
python3 dev/extensions-service/run.py <env> runtime set-profile --medium
```

**Optional:**

| Flag | When to use |
|------|-------------|
| `--large` / `--medium` / … | Preset CPU/memory |
| `--launch-type ec2` / `fargate` | Launch type |
| `--network-mode bridge` / `awsvpc` / … | ECS network mode |
| `--ec2-instance-type m5.2xlarge` | EC2 instance type |
| `--asg-min-size`, `--asg-desired-capacity`, `--asg-max-size` | ASG sizing |
| `runtime export-lambda-env` | Refresh `lambda_env_export.json` |

---

## Other commands

| Command | Purpose |
|---------|---------|
| `list` | List extensions (`extensions/<name>/package/`) |
| `setup-iam` | Lambda handlers IAM only (subset of stage 1) |
| `provision-ecs-capacity` / `undeploy-ecs-capacity` | EC2 ASG capacity without full provision |
| `deploy publish --type ecs` | Optional: mark release complete in `release_manifest.json` (no AWS) |
| `run-local <handler>` | Invoke handler locally via Docker |
| `view-logs` | CloudWatch logs for handlers Lambda |
| `test <handler>` | Invoke handler on AWS Lambda |

```bash
python3 dev/extensions-service/run.py <env> run-local <handler_name>
python3 dev/extensions-service/run.py <env> view-logs --follow
python3 dev/extensions-service/run.py <env> test <handler_name>
```

---

## State files

All under `dev/extensions-service/state/<env>/` — gitignored except `state/schemas/`.

| File | Written by | Role |
|------|-----------|------|
| `deploy_input.json` | Bootstrap merge (copy in) | **Required for stage 2** — `VARS` / `SECRETS` for deploy and handlers GitHub Environment |
| `provision_manifest.json` | `provision-infra apply` | Optional; overrides some deploy values when present |
| `runtime_profile.json` | `provision-infra apply`, `runtime set-profile` | Optional ECS sizing / launch type |
| `handlers_github_oidc.json` | `provision-infra apply` + `--github-repo` | Handlers OIDC metadata (merged into `deploy_input` at bootstrap) |
| `release_manifest.json` | `deploy build` / `push` (optional `publish`) | Local audit trail: last build, last push image URI, optional `last_publish` marker |
| `lambda_deployment.zip` | `build` | Lambda zip artifact for `deploy deploy` / `deploy update` |

| `lambda_env_export.json` | `provision-infra export`, `runtime export-lambda-env` | For `launcher/vars.json` (stage 3) |

---

## Per-extension package layout

**Platform env vs packages:** `<env>` names state and AWS resources. Handler code
is installed from dist names via `--packages` (not monorepo folder COPY).

| Path | Role |
|------|------|
| `extensions/<name>/package/` | Source for `prepare_handlers_wheelhouse --from-monorepo` / published sdists |
| `.handlers-build/wheelhouse` + `handlers-assets` | Prepare output consumed by `run.py build` |
| `extensions-service/state/<env>/lambda_deployment.zip` | Build output consumed by deploy |
| `extensions-service/state/<env>/` | Manifests + deploy input (gitignored except `schemas/`) |

---

## Optional dependencies and `[large-dependencies]`

In `extensions/<name>/package/pyproject.toml`:

- **`[project.dependencies]`** — Lambda zip (keep small)
- **`[project.optional-dependencies] large-dependencies`** — heavy libs for `build --large` / ECS image

Prepare and the ECS image install step only apply `[large-dependencies]` when the wheelhouse artifact declares that extra (`Provides-Extra` / optional-dependencies). Packages without it (e.g. triage, gro) are skipped. Shared runtime pins listed in `scripts/handlers_core_packages.py` (today: `renglo-lib`) always install in the base pip step and are never large-extra candidates.

If `pip install` fails, the build retries with `--only-binary` for packages in `wheel_libs.json`.

---

## Notes

**Handlers Lambda vs launcher backend Lambda:** This service deploys **`{ext}-handlers`** as a zip. The **backend** Lambda and its ECR repo come from **bootstrap → launcher**, not from `deploy push` here.

**`launcher/vars.json` ECS entries** — refresh with `provision-infra export` or `runtime export-lambda-env` after VPC or profile changes.

**Windows / WSL:** if shell scripts fail with `\r` errors:

```bash
sed -i 's/\r$//' dev/extensions-service/scripts/*.sh
```

## Deploy flow chart

```mermaid
flowchart LR
  subgraph provision_infra [1. provision-infra]
    P[IAM and optional ECS capacity]
    OIDC[optional GitHub OIDC roles]
  end

  subgraph deploy_block [2. deploy]
    B[build zip / ECS image]
    U[push ECS / Lambda]
  end

  subgraph runtime_block [3. runtime]
    R[ECS profile + env export]
  end

  provision_infra --> deploy_block --> runtime_block

  P -->|"writes state/"| StateProv[(provision_manifest.json)]
  P -->|"writes state/"| RunProf[(runtime_profile.json)]
  P -->|"writes state/"| LambdaExport[(lambda_env_export.json)]
  OIDC -->|"writes state/"| OidcState[(handlers_github_oidc.json)]
  B -->|"reads extensions/"| PyToml[(pyproject.toml)]
  B -->|"reads wheelhouse"| Wheels[(renglo-lib + handler pins)]
  B -->|"writes state/"| Release[(release_manifest.json)]
  B -->|"writes state/"| Zip[(lambda_deployment.zip)]
  U -->|"reads state/"| StateProv
  U -->|"reads state/"| DeployIn[(deploy_input.json - created in bootstrap)]
  U -->|"optional state/"| RunProf
  U -->|"writes state/"| Release
  R -->|"reads state/"| StateProv
  R -->|"reads/writes state/"| RunProf
  R -->|"writes state/"| LambdaExport
```
