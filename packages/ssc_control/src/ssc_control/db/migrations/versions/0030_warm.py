"""SSC-092 warm option: ``ssc.environment.warm``, the production environments an org admin keeps
at one instance, and ``ssc.warm_gateway``, the cell gateway's part and the deployer run setting it.

Revision ID: 0030_warm
Revises: 0029_egress

Downgrade drops the table and the column (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0030_warm"
down_revision = "0029_egress"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.warm_gateway;
ALTER TABLE ssc.environment
  DROP CONSTRAINT environment_warm_prod_check,
  DROP COLUMN warm;
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
    _run((SQL_DIR / "0030_warm.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
