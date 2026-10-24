-- Ticket 19D · One row per customer being onboarded, driven by the worker's onboarding.tick and
-- the operator CLI (python -m ssc_control.onboarding). Platform data like ssc.cell (db/README.md
-- rule 18): no RLS, since it exists before its org does. Two tracks: the cell (cell_state) and the
-- identity (idp_state); status is open until both are done (live) or the operator abandons it.
-- Rows are never deleted.
CREATE TABLE ssc.onboarding (
  id                     text PRIMARY KEY CHECK (id ~ '^onb_[a-z0-9]{20}$'),
  org_id                 text NOT NULL UNIQUE CHECK (org_id ~ '^org_[a-z0-9]{20}$'),
  label                  text NOT NULL UNIQUE REFERENCES ssc.cell (label)
                         CHECK (label ~ '^[a-z][a-z0-9]{7,15}$'),
  stage                  text NOT NULL CHECK (stage IN ('staging', 'prod')),
  customer_name          text NOT NULL CHECK (length(customer_name) BETWEEN 1 AND 120),
  domain                 text NOT NULL CHECK (
                           length(domain) BETWEEN 1 AND 253
                           AND domain ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'),
  admin_email            text NOT NULL CHECK (
                           length(admin_email) BETWEEN 3 AND 320
                           AND admin_email = lower(admin_email)
                           AND admin_email ~ '^[^@[:space:]]+@[^@[:space:]]+$'),
  workos_organization_id text UNIQUE CHECK (length(workos_organization_id) BETWEEN 1 AND 100),
  sso_link               text CHECK (length(sso_link) BETWEEN 1 AND 2000),
  dsync_link             text CHECK (length(dsync_link) BETWEEN 1 AND 2000),
  join_rule              text CHECK (join_rule IN ('idp_id', 'email')),
  status                 text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'live', 'abandoned')),
  cell_state             text NOT NULL DEFAULT 'awaiting_cell'
                         CHECK (cell_state IN ('awaiting_cell', 'cell_ready')),
  idp_state              text NOT NULL DEFAULT 'requested'
                         CHECK (idp_state IN ('requested', 'awaiting_idp', 'org_created')),
  reason                 text CHECK (reason IN (
                           'JOIN_RULE_CONFIRM', 'IDP_UNSUPPORTED', 'ADMIN_NOT_IN_DIRECTORY',
                           'WORKOS_ERROR')),
  operator               text NOT NULL CHECK (operator ~ '^op_[a-z0-9][a-z0-9._-]{0,62}$'),
  last_error             text CHECK (length(last_error) BETWEEN 1 AND 500),
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT onboarding_links_check CHECK (
    idp_state = 'requested'
    OR (workos_organization_id IS NOT NULL AND sso_link IS NOT NULL AND dsync_link IS NOT NULL)),
  CONSTRAINT onboarding_org_created_check CHECK (idp_state <> 'org_created' OR join_rule IS NOT NULL),
  CONSTRAINT onboarding_live_check CHECK (
    status <> 'live'
    OR (join_rule IS NOT NULL AND cell_state = 'cell_ready' AND idp_state = 'org_created')),
  CONSTRAINT onboarding_reason_waiting_check CHECK (
    reason IS NULL
    OR (status = 'open'
        AND (idp_state = 'awaiting_idp' OR (idp_state = 'requested' AND reason = 'WORKOS_ERROR'))))
);

CREATE TRIGGER onboarding_refuse_truncate
  BEFORE TRUNCATE ON ssc.onboarding
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

GRANT SELECT, INSERT, UPDATE ON ssc.onboarding TO ssc_app;

-- The welcome mail to an onboarded org's admin: a template with no approval.
ALTER TABLE ssc.notification_outbox
  DROP CONSTRAINT notification_outbox_kind_check,
  ADD CONSTRAINT notification_outbox_kind_check CHECK (
    kind IN ('arrival', 'reminder', 'digest', 'decided', 'welcome')),
  DROP CONSTRAINT notification_outbox_check1,
  ADD CONSTRAINT notification_outbox_check1 CHECK (
    (kind IN ('digest', 'welcome')) = (approval_id IS NULL));

-- Audited in the org's chain when both tracks are done.
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
    'org.onboarded'));
