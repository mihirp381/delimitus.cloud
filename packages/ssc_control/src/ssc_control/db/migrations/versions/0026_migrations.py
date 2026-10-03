"""SSC-043 rollback warning: ``migrations`` on ``ssc.build``, ``ssc.release`` and
``ssc.app_database``, and the recovery point (``recovery_at``, ``recovery_lsn``) on
``ssc.deployment``.

Revision ID: 0026_migrations
Revises: 0025_timer_start

Downgrade drops the columns (development databases only). ``ALTER TABLE`` fires no row trigger,
so the release immutability trigger does not refuse it.
"""

from pathlib import Path

from alembic import context, op

revision = "0026_migrations"
down_revision = "0025_timer_start"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.deployment
  DROP CONSTRAINT deployment_recovery_check,
  DROP COLUMN recovery_lsn,
  DROP COLUMN recovery_at;
ALTER TABLE ssc.app_database DROP COLUMN migrations;
ALTER TABLE ssc.release DROP COLUMN migrations;
ALTER TABLE ssc.build DROP COLUMN migrations;
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
    _run((SQL_DIR / "0026_migrations.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
