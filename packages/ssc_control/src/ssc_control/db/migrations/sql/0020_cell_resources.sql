-- SSC-087 · Lazy cell resources (decision 022 amendment pending).

-- A cell's database, egress proxy and data gateway are created when first needed, by the cell
-- deployer job. One row per org (one org is one cell) and resource. Ready is final: no code path
-- turns a resource off, so the app role cannot delete a row and nobody can change a ready one.
CREATE TABLE ssc.cell_resource (
  org_id          text NOT NULL REFERENCES ssc.org (id),
  resource        text NOT NULL CHECK (resource IN ('database', 'egress', 'connections')),
  state           text NOT NULL DEFAULT 'requested'
                  CHECK (state IN ('requested', 'creating', 'ready', 'failed')),
  cause           text NOT NULL CHECK (cause IN (
                    'deploy', 'egress_approved', 'connection_granted', 'file_use', 'admin')),
  actor_kind      text NOT NULL CHECK (actor_kind IN ('user', 'workload', 'schedule', 'operator', 'integration')),
  actor_id        text NOT NULL,
  actor_via_agent boolean NOT NULL DEFAULT false,
  actor_client_id text,
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  execution       text CHECK (length(execution) BETWEEN 1 AND 300),
  failure_code    text CHECK (failure_code ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  last_error      text CHECK (length(last_error) <= 200),
  requested_at    timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,
  ready_at        timestamptz,
  failed_at       timestamptz,
  PRIMARY KEY (org_id, resource),
  CHECK ((state = 'ready') = (ready_at IS NOT NULL)),
  CHECK ((state = 'failed') = (failure_code IS NOT NULL AND failed_at IS NOT NULL)),
  CHECK (state = 'requested' OR started_at IS NOT NULL)
);

CREATE TRIGGER cell_resource_ready_is_final
  BEFORE UPDATE OR DELETE ON ssc.cell_resource
  FOR EACH ROW WHEN (OLD.state = 'ready') EXECUTE FUNCTION ssc.refuse_row_change('SC008');
CREATE TRIGGER cell_resource_refuse_truncate
  BEFORE TRUNCATE ON ssc.cell_resource
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

-- A deployment waiting for a resource. The deployment owns its row: it adds it when it starts
-- waiting and removes it when it goes on or fails. The job re-defers every waiter when the
-- resource is ready or failed.
CREATE TABLE ssc.cell_resource_waiter (
  org_id        text NOT NULL,
  resource      text NOT NULL,
  deployment_id text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, resource, deployment_id),
  FOREIGN KEY (org_id, resource) REFERENCES ssc.cell_resource (org_id, resource),
  FOREIGN KEY (org_id, deployment_id) REFERENCES ssc.deployment (org_id, id) ON DELETE CASCADE
);

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
    'cell.resource_requested', 'cell.resource_ready', 'cell.resource_failed'));

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['cell_resource', 'cell_resource_waiter'] LOOP
    EXECUTE format('ALTER TABLE ssc.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE ssc.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY org_isolation ON ssc.%I USING (org_id = ssc.current_org()) '
                   'WITH CHECK (org_id = ssc.current_org())', t);
  END LOOP;
END;
$$;

GRANT SELECT, INSERT, UPDATE ON ssc.cell_resource        TO ssc_app;
GRANT SELECT, INSERT, DELETE ON ssc.cell_resource_waiter TO ssc_app;
