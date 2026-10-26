"""Decision 016 amendment 2026-10-26: drop ``approval_request_not_self``, so an org admin may
decide their own request.

Revision ID: 0041_admin_self_decide
Revises: 0040_onboarding

Downgrade restores the check ``NOT VALID`` (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0041_admin_self_decide"
down_revision = "0040_onboarding"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.approval_request
  ADD CONSTRAINT approval_request_not_self CHECK (
    state NOT IN ('approved', 'denied') OR decided_by_user_id IS DISTINCT FROM requested_by_user_id)
  NOT VALID;
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
    _run((SQL_DIR / "0041_admin_self_decide.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
