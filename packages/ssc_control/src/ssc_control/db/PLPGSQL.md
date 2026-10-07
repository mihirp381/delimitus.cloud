# PL/pgSQL in the control database

All SSC backend code is Python. PL/pgSQL is a bounded exemption, not a second backend language:
Postgres runs trigger functions and row-level security helpers, and those cannot be written in
Python. The list below is the whole allowed set. The limit is 10. The catalog test
`test_control_db.py::test_plpgsql_is_exactly_the_allowed_set` fails when schema `ssc` holds any
function that is not listed here, or any function in a language other than `plpgsql`.

Why `plpgsql` and not `sql`: a `LANGUAGE sql` function cannot RAISE with a chosen SQLSTATE. A
`sql` version of `current_org()` fails with `42704` or `22P02` instead of `SC001`, and on-call
paging keys on `SC001`.

| Function | Fires | Raises | Why it must be in the database |
|---|---|---|---|
| `current_org()` | inside every RLS policy | `SC001` when no org is bound | A NULL here would make every policy false and every query return zero rows with HTTP 200. Failing loud is the single most important line in the schema. |
| `refuse_last_org_admin()` | `BEFORE UPDATE OR DELETE ON user_account` | `SC002` | Two admins removing each other at once both read "2 admins" and both proceed. The trigger locks the org row (`FOR UPDATE`) so the pair is serialised. Since 0014 (SSC-019) a deactivation is always allowed, so directory sync is never blocked; an SSC operator restores an admin. |
| `owner_must_be_active()` | `BEFORE INSERT OR UPDATE OF owner_user_id ON app` | `SC003` | An app is created for, or transferred to, an active member. Deactivating an owner later is allowed so directory sync is never blocked. |
| `refuse_row_change('SCxxx')` | `BEFORE UPDATE OR DELETE ON release` (`SC004`), `ON audit_event` (`SC005`), `ON cell_resource WHEN (OLD.state = 'ready')` (`SC008`) | argument | Privileges are the first guard (the app role has no UPDATE or DELETE on these tables). The trigger is the second, and it also binds the owner. |
| `refuse_truncate()` | `BEFORE TRUNCATE ON release, audit_event, audit_head, cell_resource` | `SC006` | TRUNCATE is not covered by row triggers or by RLS. |
| `schedule_terminal_state()` | `BEFORE UPDATE ON schedule` | `SC007` | A deleted schedule stays deleted. Un-deleting would resurrect timers nobody expects. |
| `org_for_workos_organization(text)` | called by the auth host's `/authorize` (revision 0033, decision 029) | nothing | The work-email step finds the org whose active directory connection is a WorkOS organisation before any org is known. `directory_connection` is under forced RLS for every role, its owner too, so the function walks `org_index`, binds each org in turn and puts the caller's bind back before it returns (a `SET ssc.org` clause would need a superuser to create, since `ssc.org` is a placeholder setting; an error aborts the caller's transaction or savepoint, which undoes the binds too). `SECURITY DEFINER`, `STABLE`, `search_path = pg_catalog, ssc`, `EXECUTE` for the app role only. It answers one org id or NULL, never a row. O(orgs): fine for the pilot's hundreds of orgs; the upgrade is a global route table written with the connection. |

`SECURITY DEFINER` is refused for every other function (`catalog.SECURITY_DEFINER_FUNCTIONS`;
the same catalog test checks that none is executable by `PUBLIC`).

Everything else is Python: snapshot version bumps, audit hashing and chaining, grants
evaluation, idempotency claims, state machines. Privileges plus migrator ownership stay the
primary guard; the triggers are the second.

## Custom SQLSTATE class `SC`

Mirrored in `ssc_control.db.errors.SqlState`.

| Code | Meaning |
|---|---|
| `SC001` | No org bound to the transaction. A bug in the caller, never a normal outcome. |
| `SC002` | The last active admin of an org cannot be demoted or deleted (deactivation is allowed since 0014). |
| `SC003` | The app owner is not an active member of the org. |
| `SC004` | Release rows are immutable. |
| `SC005` | Audit rows are immutable. |
| `SC006` | TRUNCATE refused. |
| `SC007` | A deleted schedule cannot change. |
| `SC008` | A ready cell resource cannot change or be removed: no code path turns one off (SSC-087). |
