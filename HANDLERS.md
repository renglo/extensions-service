# Handlers build: prepare packages, then one builder

One path for Lambda/small:

1. Prepare `wheelhouse/` + `handlers-assets/` (router + configs from sdists).
2. `run.py build --wheelhouse/--assets/--packages`.

Host paths must be **absolute** (`WORKSPACE_ROOT`). Docker `COPY` paths stay context-relative inside extensions-service.

## Shared contract

| Concern | Owner |
| --- | --- |
| Fuse packages + Docker/zip | **extensions-service only** (`run.py build` → `build_lambda_package.sh`) |
| Prepare wheelhouse + assets | [`prepare_handlers_wheelhouse.py`](../bom-helper/scripts/prepare_handlers_wheelhouse.py) |
| Runtime `EXTERNAL_HANDLERS` | [`ops/arbitium-bom/platform_env.yml`](../arbitium-bom/platform_env.yml) overlay on SSM |
| Handler list in the zip | merged `handlers_config.json` from sdist assets |

Do **not** reimplement Docker/zip in renglo-ci or BOM scripts. Do **not** materialize into `extensions/<handle>/package` for image builds.

**`--local`** only selects platform/tag (`linux/arm64` + `:local`).

**`--large` / ECS package image** is not supported yet; same wheelhouse contract comes next.

## Monorepo DX

```bash
python ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
  --from-monorepo extensions/arbitium/package,extensions/arbitiumtriage/package \
  --out .handlers-build

cd ops/extensions-service
# Linux arm: add --local for :local. Windows amd64: omit --local so :latest matches the host.
python run.py arbitium build --no-ecs \
  --wheelhouse ../../.handlers-build/wheelhouse \
  --assets ../../.handlers-build/handlers-assets \
  --packages arbitium-lab,arbitium-triage
```

Prepare downloads **manylinux** wheels by default so a Windows host does not poison the wheelhouse for Lambda. Private deps (e.g. `renglo-gro`) need CodeArtifact or `--from-artifacts`.

`--packages` are **PyPI / dist names**, not folder handles.

## CI / BOM pins

```bash
python ops/bom-helper/scripts/download_python_packages.py handlers_bom/v0.1.0.json \
  --dest wheels --sdist --no-deps
python ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \
  --from-artifacts wheels \
  --packages arbitium-lab,arbitium-triage,renglo-gro \
  --out .handlers-build
python ops/extensions-service/run.py arbitium build --no-ecs \
  --wheelhouse .handlers-build/wheelhouse \
  --assets .handlers-build/handlers-assets \
  --packages arbitium-lab,arbitium-triage
```

[`deploy_handlers.yml`](../arbitium-bom/.github/workflows/deploy_handlers.yml) runs that flow (CA login → download → prepare → build).

## Local invoke (DEV_DOCKER)

After a multi-ext build, Flask/`EXTERNAL_HANDLERS_USE_DEV_DOCKER` uses the **shared** image base (primary / Lambda ARN stem). Bind-mount still hot-reloads `extensions/<call_ext>/package` — DX only; not the image build contract.

## Follow-ups

- ECS/`--large` on the same wheelhouse contract.
- Optional: ship router/config as wheel package-data so assets extract can shrink.
- Republish `extensions-service` pin in handlers BOM so CI clones a tree that includes this builder.
- Stanley handlers BOM needs python dist pins (workflow fails clearly until then).
