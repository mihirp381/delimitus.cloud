"""CD-6.3: the ``domain_active`` and ``domain_failed`` outbox kinds.

Revision ID: 0050_domain_mail
Revises: 0049_form_target

Downgrade restores the 0040 kinds ``NOT VALID`` (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0050_domain_mail"
down_revision = "0049_form_target"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.notification_outbox
  DROP CONSTRAINT notification_outbox_kind_check,
  ADD CONSTRAINT notification_outbox_kind_check CHECK (
    kind IN ('arrival', 'reminder', 'digest', 'decided', 'welcome')) NOT VALID,
  DROP CONSTRAINT notification_outbox_check1,
  ADD CONSTRAINT notification_outbox_check1 CHECK (
    (kind IN ('digest', 'welcome')) = (approval_id IS NULL)) NOT VALID;
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
    _run((SQL_DIR / "0050_domain_mail.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
