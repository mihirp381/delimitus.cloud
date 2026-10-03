# Restore a customer's database to a new instance

SSC-043 and SSC-059. Use this when a customer needs one app database put back to an earlier time, usually after a migration that went wrong. In the MVP, a restore is this runbook; an API comes later.

Each customer has a cell project (decisions 001 and 021) with one Cloud SQL instance of its own (`ssc-cell`). A restore never writes to that instance in place. Instead, Cloud SQL clones the instance at the chosen time into a **new instance in the same customer's project**. Then the one app database is copied back from the clone, and the clone is deleted. Every command below names that customer's cell project, and only that project. No other customer's project, instance or database is read or changed.

The first rehearsal is SSC-086 T5. Until then the steps below are untested; treat them as a draft. The checks marked **[rehearse]** are the ones that rehearsal has to confirm or correct.

Steps marked **[real]** create, change or delete cloud resources. Run them only with the founder's agreement and the customer's written request.

## Before you start

- The customer's cell project id (`ssc-c-<label>`) and the environment (`env_…`) whose database is to be restored. A restore is for one environment's database at a time.
- The app database and its role are both named `app_` followed by the environment id's 20 characters (`ssc_shared.runtime.database_name`). For example, `env_abc…` gives `app_abc…`.
- `gcloud` signed in as an operator who has Cloud SQL Admin and Storage Admin on **that cell project only**.
- The customer has agreed to a stop. The app is disabled while the database is copied back, so for a short time nobody can use it.

## 1. Pick the time

Point-in-time recovery keeps 7 days of logs (`transaction_log_retention_days=7` in `infra/ssc_infra/cell.py`). Choose a time inside that window:

- **Before a production deployment.** Every `prod` deployment of an environment with a database records a recovery point before the runtime is called. That point is `recovery_point.at` on `GET /v1/operations/{id}` and on `GET /v1/apps/{app}/environments/{env}/deployments`. `recovery_point.lsn` is the instance's write-ahead log position at that moment. When the cell agent could not report a position, `lsn` is null and `at` is the control plane's own clock; in that case pick a time a minute earlier.
- **A time the customer gives.** Convert it to UTC.

Write the time down in RFC 3339 UTC form, for example `2026-10-03T09:00:00.000Z`.

## 2. Clone the instance at that time [real]

```sh
gcloud sql instances clone ssc-cell ssc-restore-<yyyymmddhhmm> \
    --project=ssc-c-<label> --point-in-time='<time from step 1>'
```

The clone lands in the same project, region and private network, and is encrypted with the same key. The live instance and its apps keep running while the clone is made. It is billed as one more instance until step 7. **[rehearse]** Record how long the clone took and how large the instance was.

## 3. Export the one database from the clone [real]

Make a short-lived bucket in the same project. Let the clone's service account write to it:

```sh
gcloud storage buckets create gs://ssc-c-<label>-restore --project=ssc-c-<label> \
    --location=<the cell's region> --uniform-bucket-level-access
SA=$(gcloud sql instances describe ssc-restore-<yyyymmddhhmm> --project=ssc-c-<label> \
    --format='value(serviceAccountEmailAddress)')
gcloud storage buckets add-iam-policy-binding gs://ssc-c-<label>-restore \
    --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
gcloud sql export sql ssc-restore-<yyyymmddhhmm> gs://ssc-c-<label>-restore/app.sql.gz \
    --project=ssc-c-<label> --database=app_<20 characters> --clean --if-exists
```

Export only that one database, never the whole instance. The other environments' databases on the clone are left alone and are deleted with it.

## 4. Stop the app [real]

```sh
ssc disable <app>
```

The command follows the kill switch run until every step has ended. After that, nothing can write to the database while it is replaced.

## 5. Copy the database back [real]

Let the **live** instance's service account read the bucket, then import:

```sh
LIVE_SA=$(gcloud sql instances describe ssc-cell --project=ssc-c-<label> \
    --format='value(serviceAccountEmailAddress)')
gcloud storage buckets add-iam-policy-binding gs://ssc-c-<label>-restore \
    --member="serviceAccount:$LIVE_SA" --role=roles/storage.objectViewer
gcloud sql import sql ssc-cell gs://ssc-c-<label>-restore/app.sql.gz \
    --project=ssc-c-<label> --database=app_<20 characters> --user=app_<20 characters>
```

Run the import as the app's own role. The export's `DROP … IF EXISTS` statements then remove the app's current objects, and the restored ones are owned by the same role, so the app's grants and connection limit stay as they were. No other database on the instance is touched.

**[rehearse]** Confirm three things:

- The import can drop and recreate every object as the app role.
- Ownership comes out as the app role.
- The app role still cannot connect to any other database: repeat by hand the checks of `test_an_app_role_cannot_reach_beyond_its_database` (`packages/ssc_agent/tests/test_app_database.py`).

## 6. Start the app and check [real]

```sh
ssc enable <app>
ssc status <app>
```

Then:

- Open the app, and check with the customer that the data is as it was at the chosen time.
- If the restore went back past migrations, the code that is live may expect the newer schema. Put back the release that matches the restored schema: `ssc rollback <app> R<n> --env prod --confirm`. A rollback still warns here, because the platform's record of the migrations a database may have run (`ssc.app_database.migrations`) only ever grows. It does not know about the restore.

## 7. Clean up [real]

```sh
gcloud sql instances patch ssc-restore-<yyyymmddhhmm> --project=ssc-c-<label> --no-deletion-protection
gcloud sql instances delete ssc-restore-<yyyymmddhhmm> --project=ssc-c-<label>
gcloud storage rm --recursive gs://ssc-c-<label>-restore
```

Check that `gcloud sql instances list --project=ssc-c-<label>` shows only `ssc-cell`.

## 8. Write it down

Record the following in the incident note:

- the customer and environment;
- the chosen time and where it came from (a deployment's recovery point or the customer);
- the clone, export and import durations;
- how long the app was disabled;
- the customer's confirmation.

The SSC-086 T5 rehearsal adds its measured times to this page.
