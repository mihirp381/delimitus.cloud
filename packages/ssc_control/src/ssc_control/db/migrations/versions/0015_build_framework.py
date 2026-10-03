"""SSC-015 build pipeline: ``framework`` on ``ssc.build`` and ``ssc.release``.

Revision ID: 0015_build_framework
Revises: 0014_identity

Downgrade drops both columns (development databases only). ``ALTER TABLE`` fires no row
trigger, so the release immutability trigger does not refuse it.
"""

from pathlib import Path

from alembic import context, op

revision = "0015_build_framework"
down_revision = "0014_identity"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.release DROP COLUMN framework;
ALTER TABLE ssc.build DROP COLUMN framework;
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
    _run((SQL_DIR / "0015_build_framework.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
