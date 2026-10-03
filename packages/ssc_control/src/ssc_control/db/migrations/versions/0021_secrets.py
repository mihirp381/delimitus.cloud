"""SSC-026 secrets: when a secret reference was set, a numbered version only, and the secret
versions each deployment runs.

Revision ID: 0021_secrets
Revises: 0020_cell_resources

Downgrade drops the two columns and the check (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0021_secrets"
down_revision = "0020_cell_resources"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.deployment DROP COLUMN secret_refs;
ALTER TABLE ssc.secret_ref DROP CONSTRAINT secret_ref_version_check, DROP COLUMN updated_at;
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
    _run((SQL_DIR / "0021_secrets.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
