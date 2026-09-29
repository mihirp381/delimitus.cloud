"""SSC-012 anchors: ``ssc.audit_anchor``, ``org.cell_label`` for every org, and
``access_snapshot.content_digest``.

Revision ID: 0011_audit_anchor
Revises: 0010_build

Downgrade drops the table and the column and makes the label optional again, keeping the
labels (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0011_audit_anchor"
down_revision = "0010_build"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.access_snapshot DROP COLUMN content_digest;
ALTER TABLE ssc.org ALTER COLUMN cell_label DROP NOT NULL;
ALTER TABLE ssc.org ALTER COLUMN cell_label DROP DEFAULT;
DROP TABLE ssc.audit_anchor;
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
    _run((SQL_DIR / "0011_audit_anchor.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
