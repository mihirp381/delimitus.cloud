"""B2: Procrastinate 3.10.0 in schema ``procrastinate`` (vendored) and ``ssc.org_index``.

Revision ID: 0006_procrastinate_orgindex
Revises: 0005_approvals
"""

from pathlib import Path

from alembic import context, op

revision = "0006_procrastinate_orgindex"
down_revision = "0005_approvals"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
PROCRASTINATE_SCHEMA_SQL = SQL_DIR / "vendor" / "procrastinate-3.10.0-schema.sql"

ENTER_SCHEMA = """
CREATE SCHEMA procrastinate AUTHORIZATION ssc_migrate;
SET LOCAL search_path = procrastinate;
"""
LEAVE_SCHEMA = "RESET search_path;"

DOWNGRADE_SQL = """
DROP TABLE ssc.org_index;
DROP SCHEMA procrastinate CASCADE;
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
    _run(ENTER_SCHEMA)
    _run(PROCRASTINATE_SCHEMA_SQL.read_text(encoding="utf-8"))
    _run(LEAVE_SCHEMA)
    _run((SQL_DIR / "0006_procrastinate_orgindex.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
