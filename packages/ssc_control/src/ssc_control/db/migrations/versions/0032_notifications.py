"""SSC-049 approvals inbox: ``ssc.notification_outbox`` (what is still to be mailed, no address and
no text), ``approval_request.reminded_at``, the ``cli`` decision channel and ``approval.cancelled``.

Revision ID: 0032_notifications
Revises: 0031_connections

Downgrade drops the table and the column and restores the two vocabularies (development
databases only), ``NOT VALID`` because rows already written with a new value stay.
"""

from pathlib import Path

from alembic import context, op

revision = "0032_notifications"
down_revision = "0031_connections"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.notification_outbox;
ALTER TABLE ssc.approval_request
  DROP COLUMN reminded_at,
  DROP CONSTRAINT approval_request_decision_channel_check,
  ADD CONSTRAINT approval_request_decision_channel_check CHECK (
    decision_channel IN ('email', 'chat', 'console')) NOT VALID;
ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'org.updated',
    'user.created', 'user.updated', 'user.deactivated', 'user.reactivated',
    'group.synced',
    'app.created', 'app.owner_transferred', 'app.disabled', 'app.quarantined',
    'app.enabled', 'app.deleted',
    'login.succeeded', 'login.failed', 'token.issued', 'token.revoked',
    'secret.bound', 'secret.rotated', 'secret.removed',
    'grant.added', 'grant.removed',
    'bundle.stored', 'build.started', 'build.failed', 'release.created',
    'deploy.started', 'deploy.finished', 'deploy.failed',
    'rollback.started', 'rollback.finished', 'rollback.failed',
    'kill_switch.step',
    'approval.requested', 'approval.decided',
    'schedule.created', 'schedule.updated', 'schedule.paused', 'schedule.resumed',
    'schedule.deleted', 'schedule.run_requested',
    'connection.created', 'connection.removed',
    'connection.updated', 'connection.granted', 'connection.revoked',
    'connection.ceiling_lowered', 'connection.flagged',
    'operator.access',
    'audit.exported', 'audit.reanchored',
    'directory.connected', 'directory.frozen', 'identity.linked',
    'cell.resource_requested', 'cell.resource_ready', 'cell.resource_failed',
    'github.installation_bound', 'repo.connected', 'repo.disconnected')) NOT VALID;
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
    _run((SQL_DIR / "0032_notifications.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
