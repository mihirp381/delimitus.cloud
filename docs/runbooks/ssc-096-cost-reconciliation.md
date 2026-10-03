# SSC-096 monthly cost reconciliation

Once a month, this runbook compares each project's bill with the cost model and the usage events. When a line is off, the model changes, not the page. The command is `uv run python -m ssc_control.metrics.reconcile`. Its docstring gives the attribution rules. The model lives in `packages/ssc_control/src/ssc_control/metrics/cost_model.toml`, and each figure in it names its source. The proof-run kit reads the same file (`spikes/proofrun/proofrun/cost.py`).

Nothing here creates or changes a resource, and the command never calls a billing API. Steps marked **[real]** read a real bill or a real control database. Run them only with the founder's agreement. The page is for us only and never bills a customer (A6).

## Inputs

**The bill**: the calendar month (UTC), as CSV or as a JSON array of objects. Three columns are required:

| Column | Holds |
| --- | --- |
| `project_id` | the project id, for example `ssc-c-<label>` or `ssc-control-prod` |
| `service` | the billing service, for example `Cloud Run`, `Networking`, `Cloud SQL` |
| `usd` | cost **before credits**, in dollars, never negative |
| `month` (optional) | `YYYY-MM`; when present it must match `--month` |

Other columns are ignored. Rows with the same project and service are added together.

```csv
project_id,service,usd
ssc-control-prod,Cloud Run,41.20
ssc-c-<label>,Networking,22.25
```

```json
[{"project_id": "ssc-control-prod", "service": "Cloud Run", "usd": 41.20}]
```

**The cells**, which come from one of two places:
- the control database at `SSC_DATABASE_DSN`. Each org with a cell label is the cell `ssc-c-<label>`, created when the org was. Its usage comes from the `usage_hour`, `cold_start` and `fixed_resource` events. `--org` limits the run to the orgs you name;
- `--cells`, for cells that no control database knows about, such as the proof-run cells. It is a CSV with `project_id,resource,created_at`:
  - `resource` is `cell`, `database`, `egress` or `connections`;
  - `created_at` is ISO 8601 with a zone;
  - each project has exactly one `cell` row.

  These cells have no events, so they show fixed cost only.

**Reasons** (optional, `--reasons`): a JSON object that maps a line id to the written reason it is off. Line ids are printed in the page's flagged table:

| Line id | Line |
| --- | --- |
| `project:<project>` | the project's bill against the model |
| `fixed:<cell>` | the cell's fixed cost |
| `service:<platform project>:<service>` | one service on a platform project |
| `cloud_run_rest:<cell>` | the cell's Cloud Run bill not attributed to apps, against the gateway model |
| `type:<rare\|daily\|session\|heavy>` | the mean cost of one app of that type |
| `per_app` | cost per app, all in |
| `split:<rare\|daily\|heavy>` | the measured usage split against 70/20/10 |

A reason for a line that is not on the page is refused.

## Monthly steps

### 1. Export the bill **[real]**

Wait until the 5th of the following month. Late usage is still arriving before then.

In the Cloud Billing console:
1. Open the billing account and choose **Reports**.
2. Set the range to the whole month (**Invoice month** works too) and choose **Group by: Project**, then **Service** as the second dimension. If your console has no second dimension, use **Cost table**, which nests services under projects.
3. Untick every credit (**Promotions and others**, **Free tier**, **Sustained use discounts**, and so on). The model compares list prices, and the free tier would hide the first vCPU-seconds.
4. Download the table as CSV.

In a spreadsheet:
1. Keep three columns: **Project ID**, **Service description** and **Cost** (before credits).
2. Rename them `project_id`, `service` and `usd`.
3. Delete the subtotal and total rows, and save the file as `bill-YYYY-MM.csv`.

Keep the project id, not the project name. Don't use the BigQuery export.

The service names in the file must match the model's `[services]` table:
- `Cloud Run` counts as apps;
- `Networking`, `Compute Engine`, `Cloud SQL`, KMS, `Cloud DNS`, `Artifact Registry`, `Cloud Storage`, `Certificate Manager` and `Secret Manager` count as fixed;
- `Cloud Build`, `Cloud Logging` and `Cloud Monitoring` are not attributed.

If the console labels a service differently, add the new name to `[services]` in the model. Don't edit the bill.

### 2. Run the reconciliation **[real]**

At the repository root, connect to the control database read-only. The SSC-064 runbook covers `cloud-sql-proxy` and the app role. Then run:

```sh
export SSC_DATABASE_DSN=<read-only DSN, from the shell, never written to a file>
uv run python -m ssc_control.metrics.reconcile --month 2026-10 --bill bill-2026-10.csv --out results/
unset SSC_DATABASE_DSN
```

This writes `results/ssc-cost-2026-10.md` and `results/ssc-cost-2026-10.json`. Without `--out`, the page goes to stdout, and `--json` prints the JSON instead.

The command refuses a bad bill, an unknown cell project or reason id, and a cell that appears both in `--cells` and in the database.

### 3. Read the page

There are eight sections:
1. projects;
2. fixed cost per cell;
3. usage per app;
4. what is not attributed to apps, and the platform services;
5. cost per app;
6. the usage split;
7. the model's arithmetic;
8. the flagged lines.

A line is flagged when it is:
- more than 20 % off the model, or outside its band by more than 20 % of the nearest edge;
- billed with no model, such as a project the control plane does not know;
- modelled with no bill, such as a cell with no bill lines.

A money line under $1 on both sides is never flagged.

Platform projects are compared with SSC-064: prod about $70 (the parts add to $68.67) and staging about $40. Prod's Cloud Run line is the worker plus the API's minimum instance, $40.

### 4. Write a reason for every flagged line

Put the reasons in `reasons-YYYY-MM.json`, then re-run step 2 with `--reasons reasons-YYYY-MM.json`. A flagged line keeps its flag; the reason sits beside it. Commit nothing from `results/`. Keep the page and the JSON with the month's records.

### 5. Change the model when a line is off

A line that is more than 20 % off two months running, or for a reason that will recur, means the model is wrong. Change it in these three places, in the same change:

1. **`cost_model.toml`**: the figure and its `source`, saying which month's bill showed it. The model's own tests and the kit's test (`spikes/proofrun/tests/test_t8_t9_t10.py`) read this file. If you change a cell part, also update `ssc_contracts.cells.MONTHLY_USD`; a test checks that the two agree.
2. **Architecture section 10**: the ten-customer table and the per-app target, if the change moves them.
3. **Decision 006, A7**: the cost assumption, with the month and the measured figure.

Never change a stated figure so that section 7 of the page comes out clean. Section 7 lists every stated figure that its parts and rates do not reach. That gap closes when the parts change or when the stated figure is re-derived, and both go through steps 1 to 3 above.

## First run: the proof-run cells after SSC-086 T9 **[real]**

These cells have no control database record, so run with `--cells` and without `SSC_DATABASE_DSN`.

1. Export the month's bill as in step 1. Include the proof-run projects `ssc-c-$L1` and `ssc-c-$L2`, and `ssc-control-prod` if it ran that month.
2. Write `cells.csv` from the stacks. A project's creation time comes from `gcloud projects describe ssc-c-$L1 --format='value(createTime)'`. A database or proxy flag's time is the time of the `pulumi up` that turned it on, from `pulumi stack history --stack c-$L1`.

   ```csv
   project_id,resource,created_at
   ssc-c-<label 1>,cell,2026-10-06T14:00:00+00:00
   ssc-c-<label 1>,database,2026-10-08T09:30:00+00:00
   ssc-c-<label 2>,cell,2026-10-06T15:10:00+00:00
   ```

3. Run the command:

   ```sh
   uv run python -m ssc_control.metrics.reconcile --month 2026-10 --bill bill-2026-10.csv --cells cells.csv --out results/
   ```

What this run proves:
- the fixed lines: T1's empty under $25 and full under $50, now taken from a real bill with each part pro-rated from its creation;
- the platform lines.

It cannot attribute Cloud Run to apps, because these cells have no events. The whole Cloud Run bill appears as "Cloud Run, not apps", and T9's own `t9 bill` command checks the hourly rates. Write T9's usage amounts and the page's fixed lines into `spikes/proofrun/RESULTS.md`, and change the model as in step 5 where they disagree.

## Second run: dogfood (SSC-030) **[real]**

The first full month in which the dogfood org has its cell and events. Run steps 1 to 4 with the control database and `--org <dogfood org id>`. For the first time, this run shows:
- usage per app;
- the attributed Cloud Run bill;
- the gateway line;
- a measured split.

The split and cost per app will be flagged while there are fewer apps than the model's 200 and fewer than 20 typed apps. Give that as the reason; don't change the model for it. Change the model for anything else that is off, as in step 5.

## What waits for real bills

- The exact column names of the console's CSV, and which service the load balancer, NAT and fixed IP appear under (`Networking` or `Compute Engine`). Both count as fixed.
- Per-app sizes. Usage events carry no vCPU or memory, so every app is priced at the reference instance (1 vCPU, 512 MiB).
- A gateway model for requests outside sessions. The model prices the gateway only for hours with a session open.
- Cloud Logging is shown as not attributed, although the cell's $2 "basics" part includes logs.
- The measured split, which replaces 70/20/10 in `[split]` once there are 20 typed apps.
