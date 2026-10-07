"""SSC gap 7 and gap 1: OAuth at the auth host. ``ssc.oauth_client`` (self-registered public
clients, global), ``ssc.oauth_code`` (hashed authorization codes, org-scoped),
``auth_session.token_audience``, the ``console`` session kind, the ``code_reuse`` revoke reason,
three ``auth.*`` audit actions and ``ssc.org_for_workos_organization``.

Revision ID: 0033_oauth
Revises: 0032_notifications

Downgrade drops the function, the tables and the column and restores the vocabularies
(development databases only), ``NOT VALID`` because rows already written with a new value stay.
"""

from pathlib import Path

from alembic import context, op

revision = "0033_oauth"
down_revision = "0032_notifications"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP FUNCTION ssc.org_for_workos_organization(text);
DROP TABLE ssc.oauth_code;
DROP TABLE ssc.oauth_client;
ALTER TABLE ssc.auth_session
  DROP COLUMN token_audience,
  DROP CONSTRAINT auth_session_kind_check,
  ADD CONSTRAINT auth_session_kind_check CHECK (kind IN ('browser', 'cli')) NOT VALID,
  DROP CONSTRAINT auth_session_revoke_reason_check,
  ADD CONSTRAINT auth_session_revoke_reason_check CHECK (revoke_reason IN (
    'logout', 'user_deactivated', 'refresh_reuse', 'operator')) NOT VALID;
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
    _run((SQL_DIR / "0033_oauth.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
