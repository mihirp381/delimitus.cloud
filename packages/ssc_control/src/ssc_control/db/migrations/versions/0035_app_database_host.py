"""GA-4.5: an app database host may end in one dot (Cloud SQL's private DNS name, kept as listed).

Revision ID: 0035_app_database_host
Revises: 0034_connection_kinds

Downgrade restores the old pattern ``NOT VALID``, because a row recorded with a trailing dot is
never rewritten (its URLs are pinned); a later update of such a row fails the restored check
(development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0035_app_database_host"
down_revision = "0034_connection_kinds"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.app_database
  DROP CONSTRAINT app_database_host_check,
  ADD CONSTRAINT app_database_host_check
    CHECK (host ~ '^[a-z0-9]([a-z0-9.:-]{0,251}[a-z0-9])?$') NOT VALID;
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
    _run((SQL_DIR / "0035_app_database_host.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
