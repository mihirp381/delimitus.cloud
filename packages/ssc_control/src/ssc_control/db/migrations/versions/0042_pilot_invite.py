"""Console sign-in rework 2026-10-26: ``ssc.pilot_invite``, pilot invite codes, and the
``invite.created``, ``invite.revoked`` and ``invite.used`` audit actions.

Revision ID: 0042_pilot_invite
Revises: 0041_admin_self_decide

Downgrade drops the table and restores the vocabulary ``NOT VALID`` (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0042_pilot_invite"
down_revision = "0041_admin_self_decide"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.pilot_invite;
ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'org.updated',
    'user.created', 'user.updated', 'user.deactivated', 'user.reactivated',
    'group.synced',
    'app.created', 'app.owner_transferred', 'app.disabled', 'app.quarantined',
    'app.enabled', 'app.deleted',
    'login.succeeded', 'login.failed', 'token.issued', 'token.revoked',
    'auth.authorize_approved', 'auth.authorize_denied', 'auth.code_reused',
    'secret.bound', 'secret.rotated', 'secret.removed',
    'grant.added', 'grant.removed',
    'bundle.stored', 'build.started', 'build.failed', 'release.created',
    'deploy.started', 'deploy.finished', 'deploy.failed',
    'rollback.started', 'rollback.finished', 'rollback.failed',
    'kill_switch.step',
    'approval.requested', 'approval.decided', 'approval.cancelled',
    'schedule.created', 'schedule.updated', 'schedule.paused', 'schedule.resumed',
    'schedule.deleted', 'schedule.run_requested',
    'connection.created', 'connection.removed',
    'connection.updated', 'connection.granted', 'connection.revoked',
    'connection.ceiling_lowered', 'connection.flagged',
    'operator.access',
    'audit.exported', 'audit.reanchored',
    'directory.connected', 'directory.frozen', 'identity.linked',
    'cell.resource_requested', 'cell.resource_ready', 'cell.resource_failed',
    'github.installation_bound', 'repo.connected', 'repo.disconnected',
    'cell.requested', 'cell.ready', 'cell.failed', 'cell.retired',
    'org.onboarded')) NOT VALID;
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
    _run((SQL_DIR / "0042_pilot_invite.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
