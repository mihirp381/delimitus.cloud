-- SSC-047 · The GitHub App: installations bound to an org, and one connected repository per app.

-- A GitHub App installation an SSC operator bound to the org (python -m ssc_control.github
-- bind). The primary key is the installation id alone, so one installation belongs to at most
-- one org whatever the binding transaction can see. Ids only: no account name, no token. Never
-- changed or removed through the app role.
CREATE TABLE ssc.github_installation (
  installation_id bigint PRIMARY KEY CHECK (installation_id > 0),
  org_id          text NOT NULL REFERENCES ssc.org (id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, installation_id)
);

CREATE TRIGGER github_installation_refuse_truncate
  BEFORE TRUNCATE ON ssc.github_installation
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.github_installation ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.github_installation FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.github_installation USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT ON ssc.github_installation TO ssc_app;

-- The repository an app is connected to: a push to branch deploys preview. repository_id is
-- what webhooks are matched on; repository (owner/name, the owner may be a person's GitHub
-- login) is what GitHub is called with and what the connection shows. required_checks lists
-- {"name", "workflow"} objects: a check run of that name, from a run of that workflow file on
-- branch, must have passed on the commit before promote. Disconnecting deletes the row.
CREATE TABLE ssc.repo_link (
  org_id          text NOT NULL,
  app_id          text NOT NULL,
  installation_id bigint NOT NULL,
  repository_id   bigint NOT NULL CHECK (repository_id > 0),
  repository      text NOT NULL CHECK (repository ~ '^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$'),
  branch          text NOT NULL CHECK (branch ~ '^[A-Za-z0-9._/-]{1,255}$'),
  required_checks jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(required_checks) = 'array' AND jsonb_array_length(required_checks) <= 10
  ),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, app_id),
  FOREIGN KEY (org_id, app_id) REFERENCES ssc.app (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, installation_id) REFERENCES ssc.github_installation (org_id, installation_id)
);

CREATE INDEX repo_link_repository_idx ON ssc.repo_link (org_id, repository_id);

CREATE TRIGGER repo_link_refuse_truncate
  BEFORE TRUNCATE ON ssc.repo_link
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.repo_link ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.repo_link FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.repo_link USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.repo_link TO ssc_app;

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
    'github.installation_bound', 'repo.connected', 'repo.disconnected'));
