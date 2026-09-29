"""W0 lane vocabulary: the audit actions all three lanes emit, in the ``audit_event`` CHECK.

Revision ID: 0003_lane_vocab
Revises: 0002_idempotency

Expand step: the action list only grows. Downgrade restores the 0002 list and fails if a row
already uses a new action, which is correct for an append-only table.
"""

from pathlib import Path

from alembic import context, op

revision = "0003_lane_vocab"
down_revision = "0002_idempotency"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'user.created', 'user.deactivated', 'user.reactivated',
    'group.synced',
    'app.created', 'app.owner_transferred', 'app.disabled', 'app.quarantined',
    'app.enabled', 'app.deleted',
    'login.succeeded', 'login.failed', 'token.issued', 'token.revoked',
    'secret.bound', 'secret.rotated', 'secret.removed',
    'grant.added', 'grant.removed',
    'deploy.started', 'deploy.finished', 'deploy.failed',
    'rollback.started', 'rollback.finished', 'rollback.failed',
    'kill_switch.step',
    'approval.requested', 'approval.decided',
    'schedule.created', 'schedule.paused', 'schedule.resumed', 'schedule.deleted',
    'connection.created', 'connection.removed',
    'operator.access'));
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
    _run((SQL_DIR / "0003_lane_vocab.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
