"""Decision 002 amendment 2026-10-28: ``org.invite_domains``, the email domains a social org may
invite, backfilled from its people.

Revision ID: 0045_invite_domains
Revises: 0044_social_sign_in
"""

from pathlib import Path

from alembic import context, op

revision = "0045_invite_domains"
down_revision = "0044_social_sign_in"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.org
  DROP CONSTRAINT org_invite_domains_shape,
  DROP CONSTRAINT org_invite_domains_by_mode,
  DROP COLUMN invite_domains;
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
    _run((SQL_DIR / "0045_invite_domains.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
