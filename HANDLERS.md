# Handlers build: prepare packages, then one builder

One path for Lambda/small and ECS/large:

1. Prepare `wheelhouse/` + `handlers-assets/` (router + configs from sdists).
2. `run.py build --wheelhouse/--assets/--packages` (add `--large` for ECS).

Host paths must be **absolute** (`WORKSPACE_ROOT`). Docker `COPY` paths stay context-relative inside extensions-service.

## Shared contract

| Concern | Owner |
| --- | --- |
| Fuse packages + Docker/zip / ECS image | **extensions-service only** (`run.py build` → `build_lambda_package.sh`) |
| Prepare wheelhouse + assets | [`prepare_handlers_wheelhouse.py`](../bom-helper/scripts/prepare_handlers_wheelhouse.py) |
| Runtime `EXTERNAL_HANDLERS` | [`ops/arbitium-bom/platform_env.yml`](../arbitium-bom/platform_env.yml) overlay on SSM |
| Handler list in the zip/image | merged `handlers_config.json` from sdist assets |
| AWS async/batch | `*-ecs-builder` + `ecs_handler_entrypoint.py` (S3 in/out) |

Do **not** reimplement Docker/zip in renglo-ci or BOM scripts. Do **not** materialize into `extensions/<handle>/package` for image builds.

**`--local`** only selects platform/tag (`linux/arm64` + `:local`).

**`--large`** builds Lambda/small first, then `*-ecs-builder` with `[large-dependencies]`. Prepare with `--with-large-deps` so heavy wheels are in the house.

Local Docker **async/batch** is out of scope. Local smoke for large = **sync** invoke against `*-ecs-builder`.

## Monorepo DX

```bash
# Small only
python ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
  --from-monorepo extensions/arbitium/package,extensions/arbitiumtriage/package \
  --out .handlers-build

cd ops/extensions-service
# Linux arm: add --local for :local. Windows amd64: omit --local so :latest matches the host.
python run.py arbitium build --no-ecs \
  --wheelhouse ../../.handlers-build/wheelhouse \
  --assets ../../.handlers-build/handlers-assets \
  --packages arbitium-lab,arbitium-triage

# Large / ECS (same wheelhouse + heavy extras)
python ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
  --from-monorepo extensions/arbitium/package,extensions/arbitiumtriage/package \
  --with-large-deps --out .handlers-build

python run.py arbitium build --large \
  --wheelhouse ../../.handlers-build/wheelhouse \
  --assets ../../.handlers-build/handlers-assets \
  --packages arbitium-lab,arbitium-triage
```

Expect both `arbitium-lambda-builder:latest` and `arbitium-ecs-builder:latest`.

Prepare downloads **manylinux** wheels by default so a Windows host does not poison the wheelhouse for Lambda/ECS. Private deps (e.g. `renglo-gro`) need CodeArtifact or `--from-artifacts`.

`--packages` are **PyPI / dist names**, not folder handles.

## CI / BOM pins

```bash
python ops/bom-helper/scripts/download_python_packages.py handlers_bom/v0.1.0.json \
  --dest wheels --sdist --no-deps
python ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
  --from-artifacts wheels \
  --packages arbitium-lab,arbitium-triage,renglo-gro \
  --with-large-deps \
  --out .handlers-build
python ops/extensions-service/run.py arbitium build --large \
  --wheelhouse .handlers-build/wheelhouse \
  --assets .handlers-build/handlers-assets \
  --packages arbitium-lab,arbitium-triage
```

[`deploy_handlers.yml`](../arbitium-bom/.github/workflows/deploy_handlers.yml) runs that flow when `handlers_compute=ecs` (CA → download → prepare ± large deps → build → Lambda update → ECS push).

## Local invoke (DEV_DOCKER)

After a multi-ext build, Flask/`EXTERNAL_HANDLERS_USE_DEV_DOCKER` uses the **shared** image base (primary / Lambda ARN stem). ECS-listed handlers resolve to `*-ecs-builder`. Bind-mount still hot-reloads `extensions/<call_ext>/package` — DX only; not the image build contract.

## Follow-ups

- Optional: ship router/config as wheel package-data so assets extract can shrink.
- Republish `extensions-service` pin in handlers BOM so CI clones a tree that includes this builder.
- Stanley handlers BOM needs python dist pins.
- Local Docker async/batch start (not required for AWS).
