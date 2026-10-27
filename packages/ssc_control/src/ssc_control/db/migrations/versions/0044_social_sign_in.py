"""Decision 002 amendment 2026-10-27: pilot social sign-in. ``org.sign_in_mode`` and
``org.social_providers``, the ``invited`` person status, ``social`` identity links and the
``user.invited`` audit action.

Revision ID: 0044_social_sign_in
Revises: 0043_roster_sign_in_index

Downgrade restores the checks ``NOT VALID`` (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0044_social_sign_in"
down_revision = "0043_roster_sign_in_index"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
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
    'org.onboarded',
    'invite.created', 'invite.revoked', 'invite.used')) NOT VALID;
ALTER TABLE ssc.identity_link
  DROP CONSTRAINT identity_link_source_check,
  ADD CONSTRAINT identity_link_source_check CHECK (source IN ('directory', 'admin')) NOT VALID;
ALTER TABLE ssc.user_account
  DROP CONSTRAINT user_account_status_check,
  ADD CONSTRAINT user_account_status_check CHECK (status IN ('active', 'deactivated')) NOT VALID;
ALTER TABLE ssc.org
  DROP CONSTRAINT org_social_needs_provider,
  DROP COLUMN social_providers,
  DROP COLUMN sign_in_mode;
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
    _run((SQL_DIR / "0044_social_sign_in.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
