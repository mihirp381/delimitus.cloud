"""SSC-014 source upload: ``ssc.bundle``.

Revision ID: 0008_bundle
Revises: 0007_metrics_checks

Expand step: a new table only. Downgrade drops it with every bundle record (development
databases only); the blobs themselves are not touched.
"""

from pathlib import Path

from alembic import context, op

revision = "0008_bundle"
down_revision = "0007_metrics_checks"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = "DROP TABLE ssc.bundle;"


def _run(sql: str) -> None:
    if context.is_offline_mode():
        op.execute(sql)
        return
    dbapi = op.get_bind().connection.dbapi_connection
    if dbapi is None:
        raise RuntimeError("no DB-API connection behind the Alembic bind")
    dbapi.cursor().execute(sql)


def upgrade() -> None:
    _run((SQL_DIR / "0008_bundle.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
