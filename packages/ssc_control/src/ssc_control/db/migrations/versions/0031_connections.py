"""SSC-052 connections: the owner, setup status, schemas, limits and audience ceiling of a
connection, ``ssc.connection_grant`` (one environment's use of one), the ``exceed_ceiling``
approval kind and the connection audit actions.

Revision ID: 0031_connections
Revises: 0030_warm

Downgrade drops the table and the columns and restores the two vocabularies (development
databases only), ``NOT VALID`` because rows already written with a new value stay.
"""

from pathlib import Path

from alembic import context, op

revision = "0031_connections"
down_revision = "0030_warm"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.connection_grant;
ALTER TABLE ssc.approval_request
  DROP CONSTRAINT approval_request_kind_check,
  ADD CONSTRAINT approval_request_kind_check CHECK (
    kind IN ('widen_audience', 'connect_data_source', 'enable_internet_hosts', 'agent_share'))
    NOT VALID;
ALTER TABLE ssc.connection
  DROP CONSTRAINT connection_owner_fkey,
  DROP COLUMN owner_user_id,
  DROP COLUMN setup_status,
  DROP COLUMN status,
  DROP COLUMN allowed_schemas,
  DROP COLUMN limits,
  DROP COLUMN ceiling,
  DROP COLUMN updated_at;
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
    _run((SQL_DIR / "0031_connections.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
