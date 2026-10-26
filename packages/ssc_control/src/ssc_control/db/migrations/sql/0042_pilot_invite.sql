-- Console sign-in rework 2026-10-26 · Pilot invite codes (decision 029 amendment): a code an
-- operator mints for one org, entered on the console's sign-in card instead of the org id.
-- Platform data like ssc.cell (db/README.md rule 18): /authorize reads it before any org is
-- bound, so no RLS. Only the SHA-256 of a code is kept. Rows are revoked, never deleted.
CREATE TABLE ssc.pilot_invite (
  id          text PRIMARY KEY CHECK (id ~ '^pinv_[a-z0-9]{20}$'),
  code_sha256 bytea NOT NULL UNIQUE CHECK (length(code_sha256) = 32),
  org_id      text NOT NULL CHECK (org_id ~ '^org_[a-z0-9]{20}$'),
  expires_at  timestamptz NOT NULL,
  max_uses    integer NOT NULL CHECK (max_uses > 0),
  uses        integer NOT NULL DEFAULT 0 CHECK (uses >= 0 AND uses <= max_uses),
  note        text CHECK (length(note) BETWEEN 1 AND 200),
  created_by  text NOT NULL CHECK (created_by ~ '^op_[a-z0-9][a-z0-9._-]{0,62}$'),
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz,
  CHECK (expires_at > created_at)
);
CREATE INDEX pilot_invite_org ON ssc.pilot_invite (org_id, created_at);

CREATE TRIGGER pilot_invite_refuse_truncate
  BEFORE TRUNCATE ON ssc.pilot_invite
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

GRANT SELECT, INSERT, UPDATE ON ssc.pilot_invite TO ssc_app;

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
    'invite.created', 'invite.revoked', 'invite.used'));
