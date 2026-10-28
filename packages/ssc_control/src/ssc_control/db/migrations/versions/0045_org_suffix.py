"""CD-1: ``org.apps_suffix`` and ``ssc.org_suffix_retired``.

Revision ID: 0045_org_suffix
Revises: 0044_social_sign_in

Downgrade drops both (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0045_org_suffix"
down_revision = "0044_social_sign_in"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.org_suffix_retired;
ALTER TABLE ssc.org DROP COLUMN apps_suffix;
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
    _run((SQL_DIR / "0045_org_suffix.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
