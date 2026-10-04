"""SSC-053 egress proxy: ``ssc.egress_host``, the org's allowlist, and
``ssc.egress_credential``, each environment's proxy credentials (digests only).

Revision ID: 0029_egress
Revises: 0028_github

Downgrade drops both tables (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0029_egress"
down_revision = "0028_github"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.egress_credential;
DROP TABLE ssc.egress_host;
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
    _run((SQL_DIR / "0029_egress.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
