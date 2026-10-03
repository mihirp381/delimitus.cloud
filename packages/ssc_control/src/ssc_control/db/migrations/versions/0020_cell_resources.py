"""SSC-087 lazy cell resources: which of a cell's database, egress proxy and data gateway exist
or are being created, the deployments waiting for one, and three audit actions.

Revision ID: 0020_cell_resources
Revises: 0014_identity

Numbered 0020 so it cannot collide with revisions written alongside it; ``down_revision`` is
re-pointed at whichever revision is the head when it merges.

Downgrade drops the two tables and restores the audit vocabulary (development databases only),
``NOT VALID`` because audit rows already written with a new action stay.
"""

from pathlib import Path

from alembic import context, op

revision = "0020_cell_resources"
down_revision = "0014_identity"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.cell_resource_waiter;
DROP TABLE ssc.cell_resource;
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
    'directory.connected', 'directory.frozen', 'identity.linked')) NOT VALID;
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
    _run((SQL_DIR / "0020_cell_resources.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
