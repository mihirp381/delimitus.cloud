# SSC-097 restore an org's admin

An org has no active admin when its directory sync deactivates the only one (decision 024 allows that): the founder was keyed under the wrong `idp_id`, or the person left the directory. Nobody in the org can then sign in as an admin. An SSC operator restores one with a command that writes an audit record. Never use SQL for this: an update by hand leaves no trace in the audit log.

`create-org` and `connect` now refuse a founder that WorkOS does not list as an active user of the directory (`docs/runbooks/ssc-064-control-plane.md`, step 10), so this should be rare. When it happens, first find out why the person is not active in the directory, and fix the directory if the cause is there. A restored person is deactivated again by the next full sync (every 6 hours) while the directory still lacks them.

Run it with the same database role as `connect`: `ssc_app` through the proxy, as in step 10 of the control-plane runbook. There is no API route and no console button.

## Restore

1. Find the ids: the org's `org_` id and the `usr_` id of the person to restore. The founders' ids of the two production orgs are in the SSC-064 status record (step 10 of the control-plane runbook, Record).
2. Run, with `SSC_DATABASE_DSN` set:

   ```sh
   uv run python -m ssc_control.identity restore-admin --org <org_…> --user <usr_…> \
     --operator op_<your name> --reason "<why, in one short line>"
   ```

   In one transaction the command makes the person an active admin (reactivating them if they were deactivated) and writes `user.updated` (role and status before and after), `user.reactivated` when the status changed, and `operator.access`, all with you as the actor. It prints `restored <usr_…> (admin, active)`.

   The reason goes into the audit log for good, so it names no people, no emails and no text from a customer's ticket. The command refuses an `@`, any run of six or more digits and anything over 200 characters.

3. Check that the chain still verifies and shows the events:

   ```sh
   uv run python -m ssc_control.audit verify --org <org_…>
   ```

   Pass: it prints `ok: <n> events, head at seq <n>` and exits 0.

The command refuses, and writes nothing, for a user of another org, a person with no identity link under the org's directory, an org with no directory connection, and a person who is already an active admin.

**Outside the admin group.** When the org's connection names an admin group and the person is not in it, the command refuses. `--outside-admin-group` goes on anyway and warns that sync will demote them once another admin exists (the database refuses to demote the last admin, so they keep the role until then). Use it only to give the org an admin while the directory is fixed.

## Record a change that was already made by hand

The audit chain is append-only, so a change made earlier by SQL is recorded by a new event that carries the change's real time. `--already-applied-at` writes exactly one `operator.access` event (with `applied_via` `sql`, the time, your id and the reason) and changes no row. The person must already be an active admin, and the same time is recorded once.

For the founder's update of the org on 2026-10-06 at about 04:25 UTC, after step 10 is done, as `ssc_app`:

```sh
uv run python -m ssc_control.identity restore-admin --org <org_…> --user <usr_…> \
  --operator op_<your name> --already-applied-at 2026-10-06T04:25:00Z \
  --reason "founder restored own admin role by SQL after first sync deactivated them"
```

Then run the verify of step 3 of the restore.
