"""SSC-019 company login and directory sync: the directory connection, auth-host sessions,
login codes, refresh tokens, device grants and unlinked logins; deactivating the last admin.

Revision ID: 0014_identity
Revises: 0013_timers

Downgrade drops the six tables and the two columns and restores the trigger and the audit
vocabulary (development databases only). The audit chain is append-only, so the old vocabulary
comes back ``NOT VALID``: rows already written with a new action stay, new rows are checked.
"""

from pathlib import Path

from alembic import context, op

revision = "0014_identity"
down_revision = "0013_timers"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.unlinked_login;
DROP TABLE ssc.device_grant;
DROP TABLE ssc.refresh_token;
DROP TABLE ssc.login_code;
DROP TABLE ssc.auth_session;
DROP TABLE ssc.directory_connection;
ALTER TABLE ssc.identity_link DROP COLUMN source;
ALTER TABLE ssc.user_account DROP COLUMN sessions_not_before;
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
    'audit.exported', 'audit.reanchored')) NOT VALID;
CREATE OR REPLACE FUNCTION ssc.refuse_last_org_admin() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  remaining integer;
BEGIN
  IF OLD.role <> 'admin' OR OLD.status <> 'active' THEN
    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.role = 'admin' AND NEW.status = 'active' THEN
    RETURN NEW;
  END IF;
  PERFORM 1 FROM ssc.org WHERE id = OLD.org_id FOR UPDATE;
  SELECT count(*) INTO remaining
    FROM ssc.user_account
   WHERE org_id = OLD.org_id AND id <> OLD.id AND role = 'admin' AND status = 'active';
  IF remaining = 0 THEN
    RAISE EXCEPTION USING
      ERRCODE = 'SC002',
      MESSAGE = format('org %s must keep at least one active admin', OLD.org_id);
  END IF;
  RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
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
    _run((SQL_DIR / "0014_identity.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
