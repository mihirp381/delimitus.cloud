"""What the control database is declared to contain. The catalog tests compare Postgres to this.

Adding a table, a PL/pgSQL function, a privilege or a personal-data column means changing this
file in the same change, which is the point: none of those can be added by forgetting.
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
    }
)

# Secret material never lives in the control database, under any name.
FORBIDDEN_COLUMN_NAMES: Final[frozenset[str]] = frozenset(
    {"value", "secret", "secret_value", "password", "token", "plaintext", "private_key"}
)
