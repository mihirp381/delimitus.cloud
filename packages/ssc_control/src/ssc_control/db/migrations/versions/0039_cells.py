"""Ticket 19: ``ssc.cell``, the cells this control plane serves, written by the operator CLI.

Revision ID: 0039_cells
Revises: 0038_connection_preview_access

Downgrade drops the table (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0039_cells"
down_revision = "0038_connection_preview_access"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = "DROP TABLE ssc.cell;"


def _run(sql: str) -> None:
    if context.is_offline_mode():
        op.execute(sql)
        return
    dbapi = op.get_bind().connection.dbapi_connection
    if dbapi is None:
        raise RuntimeError("no DB-API connection behind the Alembic bind")
    dbapi.cursor().execute(sql)


def upgrade() -> None:
    _run((SQL_DIR / "0039_cells.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
