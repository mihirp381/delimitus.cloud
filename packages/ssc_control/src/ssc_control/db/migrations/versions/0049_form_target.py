"""CD-5.2: ``ssc.form_target``, the origins an environment's forms may post to.

Revision ID: 0049_form_target
Revises: 0048_org_domain_provider

Downgrade drops the table (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0049_form_target"
down_revision = "0048_org_domain_provider"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.form_target;
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
    _run((SQL_DIR / "0049_form_target.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
