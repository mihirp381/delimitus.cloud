"""SSC-011 idempotency claims: one org-scoped table with forced RLS and app-role privileges.

Revision ID: 0002_idempotency
Revises: 0001_control_schema

Expand step, no contract step: nothing existing changes shape. Executed through the raw psycopg
cursor for the same reason as 0001 (see that file).
"""

from pathlib import Path

from alembic import context, op

revision = "0002_idempotency"
down_revision = "0001_control_schema"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = "DROP TABLE IF EXISTS ssc.idempotency_claim CASCADE;"


def _run(sql: str) -> None:
    if context.is_offline_mode():
        op.execute(sql)
        return
    dbapi = op.get_bind().connection.dbapi_connection
    if dbapi is None:
        raise RuntimeError("no DB-API connection behind the Alembic bind")
    dbapi.cursor().execute(sql)


def upgrade() -> None:
    _run((SQL_DIR / "0002_idempotency.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
