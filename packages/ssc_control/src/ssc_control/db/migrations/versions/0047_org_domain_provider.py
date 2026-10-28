"""CD-4: ``ssc.org_domain.provider`` and ``stalled_at``, and the ``domain.delegated`` action.

Revision ID: 0047_org_domain_provider
Revises: 0046_org_domain

Downgrade drops the columns and restores the 0046 action check ``NOT VALID`` (development
databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0047_org_domain_provider"
down_revision = "0046_org_domain"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'org.updated',
    'user.created', 'user.updated', 'user.deactivated', 'user.reactivated',
    'user.invited',
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
    'org.onboarded',
    'invite.created', 'invite.revoked', 'invite.used',
    'domain.requested', 'domain.active', 'domain.failed', 'domain.flipped',
    'domain.retired')) NOT VALID;
ALTER TABLE ssc.org_domain DROP COLUMN provider, DROP COLUMN stalled_at;
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
    _run((SQL_DIR / "0047_org_domain_provider.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
