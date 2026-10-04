"""SSC-048 agent interface: ``agent_logs`` on ``ssc.org``, ``agent_client_id`` on
``ssc.device_grant`` and ``ssc.auth_session``, and the ``org.updated`` audit action.

Revision ID: 0027_agent_interface
Revises: 0026_migrations

Downgrade drops the columns and restores the audit vocabulary (development databases only),
``NOT VALID`` because audit rows already written with the new action stay.
"""

from pathlib import Path

from alembic import context, op

revision = "0027_agent_interface"
down_revision = "0026_migrations"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'user.created', 'user.updated', 'user.deactivated', 'user.reactivated',
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
    'operator.access',
    'audit.exported', 'audit.reanchored',
    'directory.connected', 'directory.frozen', 'identity.linked',
    'cell.resource_requested', 'cell.resource_ready', 'cell.resource_failed')) NOT VALID;
ALTER TABLE ssc.auth_session
  DROP CONSTRAINT auth_session_agent_check,
  DROP COLUMN agent_client_id;
ALTER TABLE ssc.device_grant DROP COLUMN agent_client_id;
ALTER TABLE ssc.org DROP COLUMN agent_logs;
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
    _run((SQL_DIR / "0027_agent_interface.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
