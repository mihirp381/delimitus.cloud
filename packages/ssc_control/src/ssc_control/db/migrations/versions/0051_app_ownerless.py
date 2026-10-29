"""Decision 019 amendment 2026-10-29: the ``app_ownerless`` outbox kind and its ``app_id``.

Revision ID: 0051_app_ownerless
Revises: 0050_domain_mail

Downgrade deletes those rows, drops the column and restores the 0050 kinds ``NOT VALID``.
"""

from pathlib import Path

from alembic import context, op

revision = "0051_app_ownerless"
down_revision = "0050_domain_mail"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.notification_outbox NO FORCE ROW LEVEL SECURITY;
DELETE FROM ssc.notification_outbox WHERE kind = 'app_ownerless';
ALTER TABLE ssc.notification_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.notification_outbox
  DROP COLUMN app_id,
  DROP CONSTRAINT notification_outbox_kind_check,
  ADD CONSTRAINT notification_outbox_kind_check CHECK (
    kind IN ('arrival', 'reminder', 'digest', 'decided', 'welcome', 'domain_active',
             'domain_failed')) NOT VALID,
  DROP CONSTRAINT notification_outbox_check1,
  ADD CONSTRAINT notification_outbox_check1 CHECK (
    (kind IN ('digest', 'welcome', 'domain_active', 'domain_failed')) = (approval_id IS NULL))
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
    _run((SQL_DIR / "0051_app_ownerless.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
