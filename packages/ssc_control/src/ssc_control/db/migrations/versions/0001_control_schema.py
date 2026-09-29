"""SSC-010 control schema: 18 org-scoped tables, forced RLS, guard triggers, app-role privileges.

Revision ID: 0001_control_schema
Revises: none

Expand step. The first revision has no contract step. The SQL lives next to this file in
``../sql/0001_control_schema.sql`` so it can be read, reviewed and diffed as SQL.

The file is executed through the raw psycopg cursor rather than ``op.execute``: it holds
PL/pgSQL bodies with ``%s`` and ``%I`` inside ``format()`` calls, and the DB-API layer would
read those as parameter placeholders. With no parameters psycopg uses the simple query
protocol, which also allows the many statements in one round trip.
"""

from pathlib import Path

from alembic import context, op

revision = "0001_control_schema"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS ssc.metrics_event, ssc.audit_head, ssc.audit_event, ssc.policy_decision,
  ssc.approval_request, ssc.connection, ssc.schedule, ssc.secret_ref, ssc.app_grant,
  ssc.deployment, ssc.release, ssc.environment, ssc.app, ssc.identity_link, ssc.group_member,
  ssc.user_group, ssc.user_account, ssc.org CASCADE;
DROP FUNCTION IF EXISTS ssc.schedule_terminal_state(), ssc.refuse_truncate(),
  ssc.refuse_row_change(), ssc.owner_must_be_active(), ssc.refuse_last_org_admin(),
  ssc.current_org();
"""


def _run(sql: str) -> None:
    if context.is_offline_mode():
        op.execute(sql)
        return
    dbapi = op.get_bind().connection.dbapi_connection
    if dbapi is None:
        raise RuntimeError("no DB-API connection behind the Alembic bind")
    dbapi.cursor().execute(sql)


def upgrade() -> None:
    _run((SQL_DIR / "0001_control_schema.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
