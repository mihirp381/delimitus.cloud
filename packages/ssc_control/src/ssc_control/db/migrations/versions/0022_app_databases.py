"""SSC-040 per-app Postgres: where each app environment's database is and its connection limit.

Revision ID: 0022_app_databases
Revises: 0021_secrets

Downgrade drops the table (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0022_app_databases"
down_revision = "0021_secrets"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.app_database;
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
    _run((SQL_DIR / "0022_app_databases.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
