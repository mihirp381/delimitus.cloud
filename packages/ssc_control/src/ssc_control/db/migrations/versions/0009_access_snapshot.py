"""SSC-021 access snapshots: ``ssc.access_snapshot`` and ``ssc.snapshot_ack``.

Revision ID: 0009_access_snapshot
Revises: 0008_bundle

Downgrade drops both tables (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0009_access_snapshot"
down_revision = "0008_bundle"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.snapshot_ack;
DROP TABLE ssc.access_snapshot;
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
    _run((SQL_DIR / "0009_access_snapshot.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
