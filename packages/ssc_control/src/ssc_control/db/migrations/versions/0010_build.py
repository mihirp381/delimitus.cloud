"""SSC-016 builds and deployments: ``ssc.build`` and ``deployment.failure_code``.

Revision ID: 0010_build
Revises: 0009_access_snapshot

Expand step: a new table, and a nullable column with CHECKs that every existing row passes.
Downgrade drops both (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0010_build"
down_revision = "0009_access_snapshot"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.deployment DROP CONSTRAINT deployment_failure_check;
ALTER TABLE ssc.deployment DROP COLUMN failure_code;
DROP TABLE ssc.build;
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
    _run((SQL_DIR / "0010_build.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
