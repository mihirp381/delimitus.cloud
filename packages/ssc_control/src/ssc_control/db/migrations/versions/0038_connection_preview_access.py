"""Ticket 14: ``ssc.connection.preview_access``, who may link it to a preview environment at once.

Revision ID: 0038_connection_preview_access
Revises: 0037_app_delete

Downgrade drops the column.
"""

from pathlib import Path

from alembic import context, op

revision = "0038_connection_preview_access"
down_revision = "0037_app_delete"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.connection NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.connection DROP COLUMN preview_access;
ALTER TABLE ssc.connection FORCE ROW LEVEL SECURITY;
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
    _run((SQL_DIR / "0038_connection_preview_access.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
