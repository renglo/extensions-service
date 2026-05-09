# Deploy flow (overview)

```mermaid
flowchart LR
  subgraph provision_infra [provision-infra]
    P[IAM and optional EC2 capacity]
  end

  subgraph deploy_block [deploy]
    B[build image / zip]
    U[push ECS / Lambda]
  end

  subgraph runtime_block [runtime]
    R[ECS profile + env export]
  end

  P -->|"writes"| StateProv[(provision_manifest.json)]
  B -->|"reads"| PyToml[(pyproject.toml)]
  B -->|"reads"| Renglo[(renglo-lib)]
  U -->|"reads"| StateProv
  U -->|"optional"| DeployIn[(deploy_input.json)]
  U -->|"optional"| RunProf[(runtime_profile.json)]
  R -->|"reads"| StateProv
  R -->|"reads/writes"| RunProf
```

## Which files each stage uses

| Stage | Main inputs |
|-------|-------------|
| **provision-infra** | Writes `state/<ext>/provision_manifest.json` and default `runtime_profile.json` if missing; reads ECS-related env from `deploy_input.json` (or `DEPLOY_INPUT_FILE`) when present to refresh the manifest. |
| **deploy build** | `extensions/<ext>/package/pyproject.toml`, `dev/renglo-lib`, extension package source. |
| **deploy push** | `provision_manifest.json`; `deploy_input.json` (for Lambda env merged into ECS task / required for Lambda deploy paths); optional `runtime_profile.json` (`ECS_PROFILE_FILE`) or ECS profile fallback. |
| **runtime** | `runtime_profile.json`, `provision_manifest.json` → writes `lambda_env_export.json`. |

State JSON files live under `dev/extensions-service/state/<extension>/`.
