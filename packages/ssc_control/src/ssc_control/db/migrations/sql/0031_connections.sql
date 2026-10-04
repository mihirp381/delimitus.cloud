-- SSC-052 · Connections, classification and the audience ceiling.

-- A connection gains an owner (who decides when an app exceeds its ceiling), a setup status
-- (pending until an admin sets it ready after the runbook's first read), a status (suspended
-- stops it at the snapshot), the schemas an app may read, its limits and its audience ceiling:
-- {"audience": "org"} or {"audience": "subjects", "subjects": [{"kind", "id"}]}. Rows from before
-- keep a null owner and the widest ceiling; the service never creates one without an owner.
-- FORCE is lifted for the foreign key's validation query and restored, as 0005 does.
ALTER TABLE ssc.connection NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.user_account NO FORCE ROW LEVEL SECURITY;

ALTER TABLE ssc.connection
  ADD COLUMN owner_user_id  text,
  ADD COLUMN setup_status   text NOT NULL DEFAULT 'pending'
    CONSTRAINT connection_setup_status_check CHECK (setup_status IN ('pending', 'ready')),
  ADD COLUMN status         text NOT NULL DEFAULT 'active'
    CONSTRAINT connection_status_check CHECK (status IN ('active', 'suspended')),
  ADD COLUMN allowed_schemas text[] NOT NULL DEFAULT ARRAY['public']
    CONSTRAINT connection_allowed_schemas_check CHECK (cardinality(allowed_schemas) BETWEEN 1 AND 50),
  ADD COLUMN limits         jsonb NOT NULL DEFAULT '{}'::jsonb
    CONSTRAINT connection_limits_check CHECK (jsonb_typeof(limits) = 'object'),
  ADD COLUMN ceiling        jsonb NOT NULL DEFAULT '{"audience": "org"}'::jsonb
    CONSTRAINT connection_ceiling_check CHECK (jsonb_typeof(ceiling) = 'object'),
  ADD COLUMN updated_at     timestamptz NOT NULL DEFAULT now(),
  ADD CONSTRAINT connection_owner_fkey FOREIGN KEY (org_id, owner_user_id)
    REFERENCES ssc.user_account (org_id, id);

ALTER TABLE ssc.connection FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.user_account FORCE ROW LEVEL SECURITY;

-- One environment's use of one connection. over_ceiling_since is set when the environment's
-- audience went beyond the connection's ceiling and cleared when it is back inside.
CREATE TABLE ssc.connection_grant (
  id                 text PRIMARY KEY CHECK (id ~ '^cgr_[a-z0-9]{20}$'),
  org_id             text NOT NULL REFERENCES ssc.org (id),
  connection_id      text NOT NULL,
  environment_id     text NOT NULL,
  limits             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(limits) = 'object'),
  over_ceiling_since timestamptz,
  created_by_user_id text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, connection_id, environment_id),
  FOREIGN KEY (org_id, connection_id) REFERENCES ssc.connection (org_id, id),
  FOREIGN KEY (org_id, environment_id) REFERENCES ssc.environment (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, created_by_user_id) REFERENCES ssc.user_account (org_id, id)
);

CREATE INDEX connection_grant_environment_idx ON ssc.connection_grant (org_id, environment_id);

CREATE TRIGGER connection_grant_refuse_truncate
  BEFORE TRUNCATE ON ssc.connection_grant
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.connection_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.connection_grant FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.connection_grant USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.connection_grant TO ssc_app;

ALTER TABLE ssc.approval_request
  DROP CONSTRAINT approval_request_kind_check,
  ADD CONSTRAINT approval_request_kind_check CHECK (
    kind IN ('widen_audience', 'connect_data_source', 'enable_internet_hosts', 'agent_share',
             'exceed_ceiling'));

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
    'connection.updated', 'connection.granted', 'connection.revoked',
    'connection.ceiling_lowered', 'connection.flagged',
    'operator.access',
    'audit.exported', 'audit.reanchored',
    'directory.connected', 'directory.frozen', 'identity.linked',
    'cell.resource_requested', 'cell.resource_ready', 'cell.resource_failed',
    'github.installation_bound', 'repo.connected', 'repo.disconnected'));
