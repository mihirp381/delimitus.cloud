-- CD-3 · An org's delegated customer domain; one row per org that is not retired.
CREATE TABLE ssc.org_domain (
  id                text PRIMARY KEY CHECK (id ~ '^dom_[a-z0-9]{20}$'),
  org_id            text NOT NULL REFERENCES ssc.org (id),
  suffix            text NOT NULL CHECK (
    suffix ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?){2,}$'
    AND length(suffix) <= 203),
  zone_name         text NOT NULL CHECK (zone_name ~ '^ssc-d-[0-9a-f]{12}$'),
  name_servers      text[] NOT NULL DEFAULT '{}' CHECK (cardinality(name_servers) <= 16),
  state             text NOT NULL DEFAULT 'pending_dns' CHECK (state IN (
    'pending_dns', 'issuing', 'active', 'failed', 'retiring', 'retired')),
  observed_ns       jsonb CHECK (jsonb_typeof(observed_ns) = 'object'),
  failure           text CHECK (length(failure) BETWEEN 1 AND 2000),
  certificate       text CHECK (length(certificate) BETWEEN 1 AND 400),
  last_delegated_at timestamptz,
  retire_after      timestamptz NOT NULL DEFAULT now() + interval '12 months',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id)
);

CREATE UNIQUE INDEX org_domain_one_live ON ssc.org_domain (org_id) WHERE state <> 'retired';
-- A name is held while its row is live or once it was ever delegated; a request that never was
-- gives it up when it fails or is retired.
CREATE UNIQUE INDEX org_domain_suffix_key ON ssc.org_domain (suffix)
  WHERE state NOT IN ('failed', 'retired') OR last_delegated_at IS NOT NULL;
CREATE UNIQUE INDEX org_domain_zone_name_key ON ssc.org_domain (zone_name)
  WHERE state NOT IN ('failed', 'retired') OR last_delegated_at IS NOT NULL;
CREATE INDEX org_domain_state ON ssc.org_domain (org_id, state);

CREATE TRIGGER org_domain_refuse_truncate
  BEFORE TRUNCATE ON ssc.org_domain
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.org_domain ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.org_domain FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.org_domain USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.org_domain TO ssc_app;

ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'org.updated',
    'user.created', 'user.updated', 'user.deactivated', 'user.reactivated',
    'user.invited',
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
    'invite.created', 'invite.revoked', 'invite.used',
    'domain.requested', 'domain.active', 'domain.failed', 'domain.flipped', 'domain.retired'));
