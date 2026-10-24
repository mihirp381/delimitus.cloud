-- Ticket 19 · The cells this control plane serves (decision 030 amendment), one row per cell,
-- written by the operator (python -m ssc_control.cells) and read live by the router. Platform
-- data, not an org's: no RLS (db/README.md rule 18). org_id is the org the cell was built for,
-- with no foreign key, since a cell may be built before its org exists. A ready cell needs its
-- public JWKS only: cells imported from SSC_CELLS have no project number.
CREATE TABLE ssc.cell (
  label          text PRIMARY KEY CHECK (label ~ '^[a-z][a-z0-9]{7,15}$'),
  stage          text NOT NULL CHECK (stage IN ('staging', 'prod')),
  status         text NOT NULL CHECK (status IN ('building', 'ready', 'failed', 'retired')),
  identity_jwks  jsonb CHECK (jsonb_typeof(identity_jwks) = 'object'),
  project_number bigint CHECK (project_number > 0),
  org_id         text UNIQUE CHECK (org_id ~ '^org_[a-z0-9]{20}$'),
  last_error     text CHECK (length(last_error) BETWEEN 1 AND 500),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT cell_ready_check CHECK (status <> 'ready' OR identity_jwks IS NOT NULL)
);

CREATE TRIGGER cell_refuse_truncate
  BEFORE TRUNCATE ON ssc.cell
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

GRANT SELECT, INSERT, UPDATE ON ssc.cell TO ssc_app;

-- The operator's writes to ssc.cell, audited in the chain of the org the cell is for.
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
    'cell.requested', 'cell.ready', 'cell.failed', 'cell.retired'));
