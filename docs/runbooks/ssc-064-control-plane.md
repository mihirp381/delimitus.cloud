# SSC-064 control plane on production

This runbook puts the API, the worker and the auth host on `ssc-control-prod` behind `api.delimitus.com`, `auth.delimitus.com` and `keys.delimitus.com`, then proves SSC-064's done-when. The program is `infra/ssc_infra/control.py`, and `infra/README.md` (Control plane) describes what it makes.

Steps marked **[real]** create or change a real resource, a registrar record or a WorkOS setting. Run them in order. The others only read.

Never write a secret value to a file, a shell history or a ticket. Each value goes from a shell variable or a generator straight into `gcloud secrets versions add --data-file=-`, and the variable is unset afterwards.

## Before you start

- A free billing slot for `ssc-control-prod` (SSC-089).
- The staging cell from SSC-086 (`<label>` below), applied with `agent_image` and the four gateway settings. Its gateway answers on `*.<label>.delimitusapps.com`.
- The WorkOS **production** environment's API key and client ID. For each tenant, Okta and Google, you also need:
  - the WorkOS organisation id (`org_01…`), with a verified domain matching the users' emails;
  - the directory id (`directory_01…`) and the SSO connection id (`conn_01…`);
  - one directory user's `idp_id` and email, who will be the founder.
- Installed: `docker buildx`, `jq`, `psql`, `openssl` and `cloud-sql-proxy`.
- Setup:
  - from the repository root: `uv sync --all-packages`;
  - in `infra`: `uv sync`, then `export PULUMI_BACKEND_URL=gs://ssc-platform-0-pulumi`.
- The org roles from `infra/README.md` (First run).
- Run `pulumi` in `infra/`, and `uv run python` and `uv run ssc` at the repository root.

```sh
P=ssc-control-prod
REG=us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform
CONN=$P:us-central1:ssc-control
LABEL=<label>
KID=auth-$(date +%Y-%m)
```

## 1. Name servers and DS records at the registrar **[real]**

Do this first. The `.com` delegation is cached for up to 48 hours, and the certificate in step 5 waits for it.

```sh
cd infra
pulumi stack output --stack platform platform_zone_name_servers
pulumi stack output --stack platform apps_zone_name_servers
```

At the registrar, set `delimitus.com`'s name servers to the first list and `delimitusapps.com`'s to the second. Delete every DS record on both domains: DNSSEC is off on both zones, and a stale DS record makes validating resolvers fail every lookup.

Pass:
- `dig +short NS delimitus.com @a.gtld-servers.net` prints the first list;
- `dig +short NS delimitusapps.com @a.gtld-servers.net` prints the second;
- `dig +short DS delimitus.com @a.gtld-servers.net` prints nothing, and so does the same query for `delimitusapps.com`.

## 2. Build and push the image **[real]**

From the repository root, on a clean checkout of the commit to release:

```sh
docker buildx build --platform linux/amd64 --provenance=false --metadata-file /tmp/control.json \
  -f packages/ssc_control/Dockerfile --tag $REG/ssc-control:$(git rev-parse --short HEAD) --push .
DIGEST=$(jq -r '."containerimage.digest"' /tmp/control.json)
echo $REG/ssc-control@$DIGEST
```

## 3. Name the stage and the cell

These are local stack settings; nothing changes until step 4.

```sh
cd infra
pulumi config set --stack platform control_stages '["prod"]'
pulumi config set --stack platform public_stage prod
pulumi config set --stack platform cell_label $LABEL
pulumi config set --stack platform cell_jwks "$(pulumi stack output --stack c-$LABEL identity_jwks)"
```

Leave `control_image`, `auth_jwks` and `auth_signing_kid` unset for now. Without them, every service runs the placeholder with no settings, the worker pool runs no instance and there is no migration job.

## 4. First apply: the project and the placeholders **[real]**

```sh
pulumi preview --stack platform --diff
pulumi up --stack platform
```

The preview should create, and change, only the following:

| Change | What |
| --- | --- |
| New project | `ssc-control-prod` (this takes the billing slot), with its APIs |
| New accounts | `ssc-control`, `ssc-control-worker`, `ssc-auth` and `ssc-control-migrate`, plus the API's and the worker's own signing grants |
| New database | Cloud SQL `ssc-control` and its database `ssc` |
| New secrets | seven empty secrets, with their grants |
| New buckets | `ssc-control-prod-blobs`, and `ssc-control-prod-keys` holding `<label>/jwks.json` |
| New Cloud Run | services `ssc-api` and `ssc-auth` on the placeholder; worker pool `ssc-worker` at 0 instances |
| New entry | the load balancer, the certificate for the three hosts, and A records for `api`, `auth` and `keys` in the `delimitus` zone |
| Deployer grants | `cell-deployer-control-runs` and `-reads` (old, on `ssc-control@ssc-control-staging`) are replaced by `cell-deployer-{staging,prod}-worker-{runs,reads}` |
| Deny rule | `ssc-deny-secret-read` now names every control account |
| On staging | `control-staging-sa` and `control-staging-signs-urls` keep their names, so they are not replaced |

If the preview replaces `control-staging-sa` or `control-staging`, stop.

If the folder's location policy refuses the global certificate, address or forwarding rules, record the error. Global Compute resources are expected to be exempt.

If the worker pool is refused because of its launch stage, record the error too.

## 5. Re-apply the cell **[real]**

The cell trusts the control plane behind the public hosts, and that is now prod.

```sh
pulumi preview --stack c-$LABEL --diff
pulumi up --stack c-$LABEL
```

The preview should change only these, from `ssc-control-staging` to `ssc-control-prod`:
- `agent-invoker` moves to `ssc-control@ssc-control-prod`;
- `bucket-control` is deleted if it is still there (SSC-012: only the worker uses the cell bucket);
- `agent-invoker-worker` and `bucket-control-worker` are new, for `ssc-control-worker@ssc-control-prod`;
- the intake's `SSC_CONTROL_SA`.

Then wait for the certificate:

```sh
gcloud compute ssl-certificates describe ssc-control-entry --global --project=$P \
  --format='value(managed.status,managed.domainStatus)'
```

Pass: `ACTIVE`, with each of the three domains `ACTIVE`. Then this prints the cell's JWKS:

```sh
diff <(curl -fsS https://keys.delimitus.com/$LABEL/jwks.json | jq -S .) \
  <(pulumi stack output --stack c-$LABEL identity_jwks | jq -S .) && echo same
```

## 6. Database roles **[real]**

The roles are made by hand, so no password is in Pulumi state.

1. Set the `postgres` password. It prompts, and the value is not kept.

   ```sh
   gcloud sql users set-password postgres --instance=ssc-control --project=$P --prompt-for-password
   ```

2. In a second terminal, start the proxy and leave it running until step 10:

   ```sh
   cloud-sql-proxy --port 5433 $CONN
   ```

3. Back in the first terminal, generate the two role passwords and set them:

   ```sh
   APP_PW=$(openssl rand -hex 24); MIGRATE_PW=$(openssl rand -hex 24)
   uv run python -c 'from ssc_control.db.roles import ENSURE_ROLES_SQL; print(ENSURE_ROLES_SQL)' \
     | psql "host=127.0.0.1 port=5433 user=postgres dbname=ssc" -v ON_ERROR_STOP=1
   printf "ALTER ROLE ssc_migrate LOGIN PASSWORD '%s';\nALTER ROLE ssc_app LOGIN PASSWORD '%s';\nGRANT CREATE ON DATABASE ssc TO ssc_migrate;\n" \
     "$MIGRATE_PW" "$APP_PW" | psql "host=127.0.0.1 port=5433 user=postgres dbname=ssc" -v ON_ERROR_STOP=1
   ```

`psql` asks for the `postgres` password. Pass: `\du` lists `ssc_app` and `ssc_migrate` with login.

## 7. Secret versions **[real]**

```sh
printf 'postgresql://ssc_app:%s@/ssc?host=/cloudsql/%s' "$APP_PW" "$CONN" \
  | gcloud secrets versions add SSC_DATABASE_DSN --project=$P --data-file=-
printf 'postgresql://ssc_migrate:%s@/ssc?host=/cloudsql/%s' "$MIGRATE_PW" "$CONN" \
  | gcloud secrets versions add SSC_MIGRATE_DSN --project=$P --data-file=-
unset MIGRATE_PW
openssl rand -base64 32 | tr -d '\n' | gcloud secrets versions add SSC_METRICS_KEY --project=$P --data-file=-
openssl rand -base64 32 | tr -d '\n' | gcloud secrets versions add SSC_AUTH_STATE_KEY --project=$P --data-file=-
read -rs WORKOS_KEY; printf '%s' "$WORKOS_KEY" \
  | gcloud secrets versions add SSC_WORKOS_API_KEY --project=$P --data-file=-; unset WORKOS_KEY
read -r WORKOS_CLIENT; printf '%s' "$WORKOS_CLIENT" \
  | gcloud secrets versions add SSC_WORKOS_CLIENT_ID --project=$P --data-file=-; unset WORKOS_CLIENT
```

The auth host's signing key is generated in memory. It goes to Secret Manager, and only its public JWKS is written to a file:

```sh
PEM=$(uv run python -c 'import sys; from ssc_control.identity.tokens import new_signing_pem; sys.stdout.write(new_signing_pem().decode())')
printf '%s\n' "$PEM" | gcloud secrets versions add SSC_AUTH_SIGNING_KEY --project=$P --data-file=-
printf '%s\n' "$PEM" | uv run python -c "import json, sys; from ssc_control.identity.tokens import Signer; print(json.dumps(Signer(sys.stdin.read().encode(), '$KID', 'https://auth.delimitus.com').jwks()))" > auth-jwks.json
unset PEM
```

The worker's timer key (SSC-041) is made the same way. Naming it makes its secret, so apply that one resource first; the worker takes the key, with `SSC_TIMER_DISPATCHER=https` and `SSC_TIMER_KEY_ID`, in step 9:

```sh
TIMER_KID=timer-$(date +%Y%m)
pulumi config set --stack platform timer_key_id $TIMER_KID
pulumi up --stack platform \
  --target 'urn:pulumi:platform::ssc-infra::gcp:secretmanager/secret:Secret::control-prod-ssc-timer-signing-key'
PEM=$(uv run python -c 'import sys; from ssc_control.timers.https import new_timer_pem; sys.stdout.write(new_timer_pem().decode())')
printf '%s\n' "$PEM" | gcloud secrets versions add SSC_TIMER_SIGNING_KEY --project=$P --data-file=-
printf '%s\n' "$PEM" | uv run python -c "import json, sys; from ssc_control.timers.https import ScheduleSigner; print(json.dumps(ScheduleSigner(sys.stdin.read().encode(), '$TIMER_KID').jwks()))" > timer-jwks.json
unset PEM
```

The cell's gateway trusts that key once its `timer_jwks` is set. The preview should change only the gateway's `SSC_TIMER_JWKS`:

```sh
pulumi config set --stack c-$LABEL timer_jwks "$(cat timer-jwks.json)"
pulumi up --stack c-$LABEL
```

Pass: `gcloud secrets versions list SSC_TIMER_SIGNING_KEY --project=$P` shows one enabled version, and `gcloud secrets get-iam-policy SSC_TIMER_SIGNING_KEY --project=$P` names `ssc-control-worker` alone.

Keep `APP_PW` in this shell for step 10, then unset it.

## 8. The release, and the migration job **[real]**

```sh
pulumi config set --stack platform control_image $REG/ssc-control@$DIGEST
pulumi config set --stack platform auth_jwks "$(cat auth-jwks.json)"
pulumi config set --stack platform auth_signing_kid $KID
pulumi up --stack platform \
  --target 'urn:pulumi:platform::ssc-infra::gcp:cloudrunv2/job:Job::control-prod-ssc-control-migrate'
gcloud run jobs execute ssc-control-migrate --project=$P --region=us-central1 --wait
```

Pass: the execution succeeds, and the job's log ends with Alembic at the head revision. The job runs as `ssc-control-migrate`, the only account that can read `SSC_MIGRATE_DSN`.

## 9. Full apply: the release runs **[real]**

```sh
pulumi up --stack platform
```

`ssc-api` (minimum 1) and `ssc-auth` move to the image with their settings and secrets. `ssc-worker` gets 1 instance. The worker also gets `SSC_CELL_DEPLOYER` once `deployer_image` is set.

Pass:
- `curl -fsS https://api.delimitus.com/healthz` and `curl -fsS https://auth.delimitus.com/healthz` both answer;
- this prints `same`:

  ```sh
  curl -fsS https://auth.delimitus.com/.well-known/jwks.json | jq -S . | diff - <(jq -S . auth-jwks.json) && echo same
  ```

A secret version added later reaches a service only with a new revision. To roll one, run `pulumi up` after a release change, or `gcloud run services update <service> --project=$P --region=us-central1 --update-labels=rolled=$(date +%s)`.

## 10. WorkOS and the two orgs **[real]**

1. In the WorkOS production dashboard, add `https://auth.delimitus.com/callback` as a redirect URI.
2. With the proxy still running, create each org as `ssc_app`, with the founder keyed under the directory. Then record its connection. Run this once for Okta (`JOIN=idp_id`) and once for Google (`JOIN=email`):

   ```sh
   export SSC_DATABASE_DSN="postgresql://ssc_app:$APP_PW@127.0.0.1:5433/ssc"
   uv run python -c '
   import asyncio, os, sys
   from ssc_control.db import NewOrg, create_org, make_engine
   from ssc_control.identity.connections import directory_issuer
   name, founder, email, directory, subject = sys.argv[1:]
   async def main():
       engine = make_engine(os.environ["SSC_DATABASE_DSN"])
       spec = NewOrg(name, founder, email, directory_issuer(directory), subject)
       print((await create_org(engine, spec)).org_id)
       await engine.dispose()
   asyncio.run(main())
   ' "<org name>" "<founder name>" "<founder email>" <directory_01…> <founder idp_id>
   uv run python -m ssc_control.identity connect --org <org_…> --operator op_<your name> \
     --workos-org <org_01…> --directory <directory_01…> --sso <conn_01…> --join-rule $JOIN
   ```

3. Choose the org whose users test the gateway in step 11c. The label is unique, so only one org can have it. Point that org at the cell. The auth host redeems a login code only for the gateway in the org's cell project, `ssc-c-<cell label>`, so a wrong label refuses every sign-in with "caller project mismatch":

   ```sh
   psql "$SSC_DATABASE_DSN" -v ON_ERROR_STOP=1 \
     -c "begin; select set_config('ssc.org', '<org_…>', true); update ssc.org set cell_label = '$LABEL' where id = '<org_…>'; commit;"
   ```

4. Set the cell's gateway to that org, then re-apply the cell:

   ```sh
   pulumi config set --stack c-$LABEL org_id <org_…>
   pulumi up --stack c-$LABEL
   ```

5. Finish up:

   ```sh
   unset APP_PW SSC_DATABASE_DSN
   ```

   Stop the proxy.

## 11. Done-when checks

**a. The sync runs every minute in the worker.**

```sh
gcloud logging read 'resource.labels.worker_pool_name="ssc-worker" AND textPayload:"directory sync"' \
  --project=$P --freshness=10m --format='value(timestamp,textPayload)'
```

Pass: one `directory sync` line per connected org each minute, and the first sync lists each directory in full. The log resource type of worker pools is not pinned here. If nothing matches, drop the label filter and search for `textPayload:"directory sync"`.

**b. `ssc login --org` against `api.delimitus.com`, for Okta and then Google.** Use a directory user who is not the founder:

```sh
uv run ssc login --org <org_…>
uv run ssc whoami
```

Pass: the browser signs in through the IdP, the code is approved, and `whoami` names a `usr_` id in that org. A refusal shows in `ssc-auth`'s log as `login refused …`.

**c. The Google part of `docs/runbooks/ssc-019-login.md`.** Run its steps 2 and 3, and the email-change check of step 4, for the Google org. Use the command lines above in place of `--api http://127.0.0.1:8000 … --auth-url …`, and the `psql` of step 10 (through the proxy, as `ssc_app`, bound with `set_config('ssc.org', …)`) in place of the rig's superuser. In detail:
- **Login.** Done in b.
- **Suspension.** Suspend the user in Google. `whoami` must fail within 5 minutes of the WorkOS event.
- **Rename.** Change the user's primary email in Google, then log in again once the directory has caught up. The `usr_` id and the people count must be unchanged.

**d. A gateway started from zero redeems a login code through its public load balancer.** This uses an app deployed in the org from step 10.3, such as SSC-086 T2's.

1. Leave the cell idle for 20 minutes, until Cloud Run has scaled the gateway to zero. Check that this prints nothing:

   ```sh
   gcloud logging read 'resource.labels.service_name="ssc-gateway"' --project=ssc-c-$LABEL --freshness=15m --limit=1
   ```

2. In a fresh browser profile, open `https://<slug>.$LABEL.delimitusapps.com/`.

Pass:
- the browser goes to `auth.delimitus.com`, signs in, and lands back on the app;
- the gateway's first log lines are its start, within seconds before the request;
- `ssc-auth`'s log shows `POST /internal/redeem` answered 200. The gateway reaches it from the cell through the `auth.delimitus.com` bypass rule and the NAT, with an ID token whose audience is `https://auth.delimitus.com`.

**e. No secret is readable by a service that does not use it.** The program side is `infra/tests/test_control.py::test_no_account_can_read_a_secret_it_does_not_use`. On the cloud:

1. Grant yourself `roles/iam.serviceAccountTokenCreator` on the four accounts through just-in-time access **[real]**.
2. Run:

   ```sh
   for pair in ssc-control:SSC_WORKOS_API_KEY ssc-control:SSC_WORKOS_CLIENT_ID ssc-control:SSC_AUTH_SIGNING_KEY \
       ssc-control:SSC_AUTH_STATE_KEY ssc-control:SSC_MIGRATE_DSN ssc-control-worker:SSC_AUTH_SIGNING_KEY \
       ssc-control-worker:SSC_AUTH_STATE_KEY ssc-control-worker:SSC_MIGRATE_DSN ssc-auth:SSC_METRICS_KEY \
       ssc-auth:SSC_MIGRATE_DSN ssc-control-migrate:SSC_DATABASE_DSN ssc-control-migrate:SSC_WORKOS_API_KEY \
       ssc-control-migrate:SSC_AUTH_SIGNING_KEY; do
     sa=${pair%%:*}; secret=${pair#*:}
     gcloud secrets versions access latest --secret=$secret --project=$P \
       --impersonate-service-account=$sa@$P.iam.gserviceaccount.com >/dev/null 2>/tmp/deny.txt \
       && echo "READABLE $pair" || { grep -q PERMISSION_DENIED /tmp/deny.txt && echo "denied $pair"; }
   done
   for s in SSC_DATABASE_DSN SSC_MIGRATE_DSN SSC_METRICS_KEY SSC_WORKOS_API_KEY SSC_WORKOS_CLIENT_ID \
       SSC_AUTH_SIGNING_KEY SSC_AUTH_STATE_KEY; do
     echo "$s"; gcloud secrets get-iam-policy $s --project=$P --format='value(bindings.members)'
   done
   gcloud projects get-iam-policy $P --format=json | jq '.bindings[] | select(.role | test("secretmanager"))'
   ```

Pass:
- every pair prints `denied`;
- each secret's members are exactly the readers in `infra/README.md` (Control plane);
- the project has no Secret Manager binding.

Then drop the token-creator grants **[real]**.

**f. The resource flags.** A non-admin is refused and an admin's change is audited. `packages/ssc_control/tests/test_cell_resources.py::test_an_admin_turns_a_resource_on_and_it_is_audited` covers this. The warm flag's refusals and audit are covered by `packages/ssc_control/tests/test_warm.py::test_members_agents_previews_and_a_wrong_cost_are_refused` and `::test_one_warm_environment_takes_one_pass_and_nothing_else`.

## Record

Add the following to the SSC-064 status in the tickets file:
- the image digest;
- the certificate's issue time;
- the org ids and the `usr_` ids;
- the lockout and rename times;
- the pass or fail line of each check.

Note anything that differed from step 4's preview list.
