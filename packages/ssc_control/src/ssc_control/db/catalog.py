"""What the control database is declared to contain. The catalog tests compare Postgres to this.

Adding a table, a PL/pgSQL function, a privilege or a personal-data column means changing this
file in the same change, which is the point: none of those can be added by forgetting.

Nineteen tables after revision 0002: the eighteen of SSC-010 plus ``idempotency_claim``
(SSC-011), which is keyed by ``(org_id, credential_id, key)`` and therefore in ``UNKEYED_TABLES``.

Revision 0006 adds ``org_index``, the single table in ``UNSCOPED_TABLES``: org ids only, no RLS,
so workers can find every org and then bind each one (decision 009 amendment). It is kept out of
``TABLES`` so that every rule stated over ``TABLES`` stays true without an exception.
Procrastinate's tables live in schema ``procrastinate`` and are not in this catalog at all.

Revision 0008 adds ``bundle`` (SSC-014), an ordinary org-scoped table.

Revision 0009 adds ``access_snapshot`` and ``snapshot_ack`` (SSC-021), keyed by org and version
and by org alone, so both are in ``UNKEYED_TABLES``.

Revision 0010 adds ``build`` (SSC-016), an ordinary org-scoped table.

Revision 0011 adds ``audit_anchor`` (SSC-012), keyed by org and time, append-only like the log,
so it is in ``UNKEYED_TABLES``.

Revision 0012 adds ``kill_switch_run`` (SSC-025), an ordinary org-scoped table.
Revision 0013 adds ``timer_run`` (SSC-041), an ordinary org-scoped table.
Revision 0014 adds ``directory_connection``, ``auth_session``, ``login_code``,
``refresh_token``, ``device_grant`` and ``unlinked_login`` (SSC-019), all ordinary org-scoped
tables.
Revision 0020 adds ``cell_resource`` and ``cell_resource_waiter`` (SSC-087), keyed by org and
resource (one org is one cell), so both are in ``UNKEYED_TABLES``.
Revision 0021 adds columns only (SSC-026): ``secret_ref.updated_at`` and
``deployment.secret_refs``, references and version numbers, never a secret value.
Revision 0022 adds ``app_database`` (SSC-040), keyed by org and environment, so it is in
``UNKEYED_TABLES``: where an app database is, never its password.
Revision 0023 adds ``usage_collection`` (SSC-028), keyed by org alone, so it is in
``UNKEYED_TABLES``, and ``environment_id`` and ``dedup_key`` on ``metrics_event``: counts and
durations of usage, never request content, paths, user ids or IP addresses.
Revision 0024 adds a column only (SSC-090): ``environment.request_timeout_seconds``.
Revision 0028 adds ``github_installation`` (SSC-047), keyed by the GitHub installation id, and
``repo_link``, keyed by org and app, so both are in ``UNKEYED_TABLES``: ids, a repository name
and a branch, never a GitHub token or key.
Revision 0029 adds ``egress_host`` and ``egress_credential`` (SSC-053), keyed by org and host
and by org, environment and credential, so both are in ``UNKEYED_TABLES``: the allowlist and
digests of proxy tokens, never a token.
Revision 0030 adds ``environment.warm`` and ``warm_gateway`` (SSC-092), keyed by org, so it is in
``UNKEYED_TABLES``: whether the gateway is kept warm and the deployer run setting it.
Revision 0031 adds ``connection_grant`` (SSC-052), keyed by a ``cgr_`` id, and columns on
``connection``: an owner, a setup status, schemas, limits and an audience ceiling. A connection's
address stays out of every read, and no credential is stored here.
Revision 0032 adds ``notification_outbox`` (SSC-049), keyed by an ``ntf_`` id: a recipient, a
template and a state, never an address or a message.
Revision 0033 adds ``oauth_code`` (OAuth for remote MCP and the console), keyed by an ``oac_``
id: digests of authorization codes, never a code. It also adds ``oauth_client``, the single table
in ``GLOBAL_TABLES``: self-registered OAuth clients, which belong to no org (a client registers
before anyone signs in), so it has no ``org_id`` and no RLS and holds nothing of any org. And it
adds ``org_for_workos_organization``, the single function in ``SECURITY_DEFINER_FUNCTIONS``.
"""

from collections.abc import Mapping
from typing import Final

SCHEMA: Final = "ssc"
MIGRATION_LEDGER: Final = "alembic_version"

TABLES: Final[frozenset[str]] = frozenset(
    {
        "org",
        "user_account",
        "user_group",
        "group_member",
        "identity_link",
        "app",
        "environment",
        "release",
        "deployment",
        "app_grant",
        "secret_ref",
        "schedule",
        "connection",
        "approval_request",
        "policy_decision",
        "audit_event",
        "audit_head",
        "metrics_event",
        "idempotency_claim",  # SSC-011, revision 0002
        "bundle",  # SSC-014, revision 0008
        "access_snapshot",  # SSC-021, revision 0009
        "snapshot_ack",  # SSC-021, revision 0009
        "build",  # SSC-016, revision 0010
        "audit_anchor",  # SSC-012, revision 0011
        "kill_switch_run",  # SSC-025, revision 0012
        "timer_run",  # SSC-041, revision 0013
        "directory_connection",  # SSC-019, revision 0014
        "auth_session",  # SSC-019, revision 0014
        "login_code",  # SSC-019, revision 0014
        "refresh_token",  # SSC-019, revision 0014
        "device_grant",  # SSC-019, revision 0014
        "unlinked_login",  # SSC-019, revision 0014
        "cell_resource",  # SSC-087, revision 0020
        "cell_resource_waiter",  # SSC-087, revision 0020
        "app_database",  # SSC-040, revision 0022
        "usage_collection",
        "github_installation",
        "repo_link",
        "egress_host",  # SSC-053, revision 0029
        "egress_credential",  # SSC-053, revision 0029
        "warm_gateway",
        "connection_grant",  # SSC-052, revision 0031
        "notification_outbox",  # SSC-049, revision 0032
        "oauth_code",  # SSC gap 7, revision 0033
    }
)

# The one exception to "org_id plus forced RLS": org ids only, readable by the app role across
# orgs, insert-only (create_org). Decision 009 amendment; db/README.md rule 14.
UNSCOPED_TABLES: Final[tuple[str, ...]] = ("org_index",)

# Tables that hold no org's data at all, so they have no org_id and no RLS: self-registered OAuth
# clients (revision 0033, decision 029; db/README.md rule 17). Kept out of TABLES like org_index.
GLOBAL_TABLES: Final[tuple[str, ...]] = ("oauth_client",)

# Tables keyed by something other than a type-prefixed id; they have no (org_id, id) pair.
UNKEYED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "group_member",
        "audit_event",
        "audit_head",
        "metrics_event",
        "idempotency_claim",
        "access_snapshot",
        "snapshot_ack",
        "audit_anchor",
        "cell_resource",
        "cell_resource_waiter",
        "app_database",
        "usage_collection",
        "github_installation",
        "repo_link",
        "egress_host",
        "egress_credential",
        "warm_gateway",
    }
)

# The bounded PL/pgSQL exemption from the Python rule. Listed and explained in PLPGSQL.md.
PLPGSQL_LIMIT: Final = 10
PLPGSQL_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "current_org",
        "refuse_last_org_admin",
        "owner_must_be_active",
        "refuse_row_change",
        "refuse_truncate",
        "schedule_terminal_state",
        "org_for_workos_organization",
    }
)
# The PL/pgSQL functions that run as their owner. Each binds orgs its caller did not name, so each
# is listed here, executable by the app role only (never PUBLIC) and explained in PLPGSQL.md.
SECURITY_DEFINER_FUNCTIONS: Final[frozenset[str]] = frozenset({"org_for_workos_organization"})

# Privileges of the application role, per table. Nothing on the migration ledger.
APP_ROLE_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = {
    "org": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "user_account": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "user_group": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "group_member": frozenset({"SELECT", "INSERT", "DELETE"}),
    "identity_link": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "app": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "environment": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "release": frozenset({"SELECT", "INSERT"}),
    "deployment": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "app_grant": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "secret_ref": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "schedule": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "connection": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "approval_request": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "policy_decision": frozenset({"SELECT", "INSERT"}),
    "audit_event": frozenset({"SELECT", "INSERT"}),
    "audit_head": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "metrics_event": frozenset({"SELECT", "INSERT"}),
    "idempotency_claim": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "bundle": frozenset({"SELECT", "INSERT", "UPDATE"}),  # pending -> stored, never DELETE
    "access_snapshot": frozenset({"SELECT", "INSERT"}),  # a published version never changes
    "snapshot_ack": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "build": frozenset({"SELECT", "INSERT", "UPDATE"}),  # queued -> running -> done, never DELETE
    "audit_anchor": frozenset({"SELECT", "INSERT"}),  # append-only, like the log
    "kill_switch_run": frozenset({"SELECT", "INSERT", "UPDATE"}),  # evidence, never DELETE
    "timer_run": frozenset({"SELECT", "INSERT", "UPDATE"}),  # queued -> running -> done
    "directory_connection": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "auth_session": frozenset({"SELECT", "INSERT", "UPDATE"}),  # revoked, never deleted
    "login_code": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),  # expired ones pruned
    "refresh_token": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),  # pruned with expiry
    "device_grant": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),  # expired ones pruned
    "unlinked_login": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "cell_resource": frozenset({"SELECT", "INSERT", "UPDATE"}),  # never turned off, never DELETE
    "cell_resource_waiter": frozenset({"SELECT", "INSERT", "DELETE"}),
    "app_database": frozenset({"SELECT", "INSERT", "UPDATE"}),  # never removed by the app
    "usage_collection": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "github_installation": frozenset({"SELECT", "INSERT"}),
    "repo_link": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "egress_host": frozenset({"SELECT", "INSERT", "DELETE"}),  # an admin removes a host
    "egress_credential": frozenset({"SELECT", "INSERT", "DELETE"}),  # older ones pruned
    "warm_gateway": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "connection_grant": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "notification_outbox": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),  # sent ones pruned
    "oauth_code": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),  # expired ones pruned
    "oauth_client": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),  # unused ones pruned
    "org_index": frozenset({"SELECT", "INSERT"}),  # unscoped; never UPDATE or DELETE
}

# Procrastinate's schema (revision 0006): the app role's privileges on its tables and sequences.
# Vendored third-party SQL, outside the org-scoped catalog; its PL/pgSQL is not ours to count.
QUEUE_SCHEMA: Final = "procrastinate"
QUEUE_APP_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = {
    "procrastinate_jobs": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "procrastinate_workers": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "procrastinate_periodic_defers": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "procrastinate_events": frozenset({"SELECT", "INSERT"}),
    "procrastinate_jobs_id_seq": frozenset({"USAGE"}),
    "procrastinate_periodic_defers_id_seq": frozenset({"USAGE"}),
    "procrastinate_events_id_seq": frozenset({"USAGE"}),
}

# Personal data, by (table, column). Explained in PII.md. Any column with one of the names in
# PII_COLUMN_NAMES must appear here, and every entry here must exist.
PII_COLUMNS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("user_account", "display_name"),
        ("user_account", "email"),
        ("user_group", "display_name"),
        ("identity_link", "subject"),
        ("audit_event", "actor_ip"),
        ("approval_request", "decision_reason"),
        ("unlinked_login", "subject"),
        ("unlinked_login", "email"),
        ("repo_link", "repository"),
    }
)
PII_COLUMN_NAMES: Final[frozenset[str]] = frozenset(
    {
        "display_name",
        "email",
        "subject",
        "actor_ip",
        "ip_address",
        "given_name",
        "family_name",
        "phone",
        "decision_reason",
        "repository",
    }
)

# Secret material never lives in the control database, under any name.
FORBIDDEN_COLUMN_NAMES: Final[frozenset[str]] = frozenset(
    {"value", "secret", "secret_value", "password", "token", "plaintext", "private_key"}
)
