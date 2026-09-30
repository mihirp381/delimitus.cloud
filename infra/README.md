# infra

Pulumi in Python for SSC on Google Cloud (SSC-013, decisions 021 and 022). This is a separate uv project outside the workspace, so the provider SDK never reaches the services.

| Stack | What it holds |
| --- | --- |
| `platform` | Folders `ssc-cells/{prod,staging}` and `ssc-sandbox`, with logs in `us-central1`. The location policy. `ssc-control-staging` and its `ssc-control` identity. The folder rule denying secret reads. Just-in-time staff access. The $250 monthly budget. |
| `c-<cell label>` | One cell: project `ssc-c-<label>`, VPC, NATs, Cloud SQL, Artifact Registry, KMS, bucket, gateway, cell agent, internal load balancer and the cell's own deny rule. |

State lives in `gs://ssc-platform-0-pulumi`, and secrets are encrypted with the KMS key `ssc-platform/pulumi-secrets`. Every call's quota goes to `ssc-platform-0`. The tools refuse any command that names `ristretto-506621`.

## First run

You need these org roles:
- `resourcemanager.folderAdmin`
- `iam.denyAdmin`
- `logging.admin`
- `orgpolicy.policyAdmin`
- `privilegedaccessmanager.admin`

You also need Application Default Credentials.

```
cd infra && uv sync
uv run python -m ssc_infra.bootstrap            # folder, project, APIs, bucket, key, platform stack
export PULUMI_BACKEND_URL=gs://ssc-platform-0-pulumi
pulumi up --stack platform
```

## A cell

```
uv run python -m ssc_infra.bootstrap cell testcell01 --probe
pulumi up --stack c-testcell01
```

A staging cell can be destroyed with `pulumi destroy --stack c-<label>`. A prod cell's project, database and services are protected. A deleted project ID stays reserved for 30 days.

## Done-when checks

```
uv run python -m ssc_infra.cell_diff testcell01 testcell02   # two cells identical
uv run python -m ssc_infra.deny_probe testcell01             # secret read denied (needs --probe)
uv run python -m ssc_infra.snapshot_rtt testcell01           # snapshot round trip under 5 s
```

`uv run pytest` runs both programs against Pulumi mocks, with no cloud.
