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
    }
)

# The one exception to "org_id plus forced RLS": org ids only, readable by the app role across
# orgs, insert-only (create_org). Decision 009 amendment; db/README.md rule 14.
UNSCOPED_TABLES: Final[tuple[str, ...]] = ("org_index",)

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
    }
)

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
    }
)

# Secret material never lives in the control database, under any name.
FORBIDDEN_COLUMN_NAMES: Final[frozenset[str]] = frozenset(
    {"value", "secret", "secret_value", "password", "token", "plaintext", "private_key"}
)
