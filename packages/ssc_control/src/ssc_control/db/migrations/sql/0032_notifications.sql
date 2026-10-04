-- SSC-049 · Approvals inbox.

-- A decision may now come from the command line (channel 'cli'), and a request remembers when
-- its three-day reminder went out so it is sent once.
ALTER TABLE ssc.approval_request
  DROP CONSTRAINT approval_request_decision_channel_check,
  ADD CONSTRAINT approval_request_decision_channel_check CHECK (
    decision_channel IN ('email', 'chat', 'console', 'cli')),
  ADD COLUMN reminded_at timestamptz;

-- What still has to be mailed. A row names a recipient and a template and nothing else: no
-- address and no text, so no personal data is copied; the worker reads the address when it sends.
-- dedupe_key makes every notification idempotent: a repeated job or a stalled retry inserts nothing.
CREATE TABLE ssc.notification_outbox (
  id              text PRIMARY KEY CHECK (id ~ '^ntf_[a-z0-9]{20}$'),
  org_id          text NOT NULL REFERENCES ssc.org (id),
  user_id         text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('arrival', 'reminder', 'digest', 'decided')),
  approval_id     text,
  dedupe_key      text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 200),
  state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sent', 'failed')),
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  sent_at         timestamptz,
  CHECK ((state = 'sent') = (sent_at IS NOT NULL)),
  CHECK ((kind = 'digest') = (approval_id IS NULL)),
  UNIQUE (org_id, id),
  UNIQUE (org_id, dedupe_key),
  FOREIGN KEY (org_id, user_id) REFERENCES ssc.user_account (org_id, id),
  FOREIGN KEY (org_id, approval_id) REFERENCES ssc.approval_request (org_id, id)
);

CREATE INDEX notification_outbox_due_idx
  ON ssc.notification_outbox (org_id, next_attempt_at) WHERE state = 'pending';

CREATE TRIGGER notification_outbox_refuse_truncate
  BEFORE TRUNCATE ON ssc.notification_outbox
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.notification_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.notification_outbox USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.notification_outbox TO ssc_app;

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
    'github.installation_bound', 'repo.connected', 'repo.disconnected'));
