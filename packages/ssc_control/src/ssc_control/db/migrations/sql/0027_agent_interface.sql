-- SSC-048 · The agent interface: agent logins at the auth host and the org's logs switch.

-- Whether an agent credential may read logs (the API's logs route and the MCP get_logs tool).
-- On until an org admin turns it off; a person's own credential is never affected.
ALTER TABLE ssc.org
  ADD COLUMN agent_logs boolean NOT NULL DEFAULT true;

-- The coding agent a command-line login was made for (`ssc login --agent claude-code`). The
-- person approves it by name on the auth host; every access token of the session then carries
-- agent=true and this client id, so every call is recorded as the agent's.
ALTER TABLE ssc.device_grant
  ADD COLUMN agent_client_id text CHECK (agent_client_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$');

ALTER TABLE ssc.auth_session
  ADD COLUMN agent_client_id text CHECK (agent_client_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
  ADD CONSTRAINT auth_session_agent_check CHECK (agent_client_id IS NULL OR kind = 'cli');

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
    'cell.resource_requested', 'cell.resource_ready', 'cell.resource_failed'));
