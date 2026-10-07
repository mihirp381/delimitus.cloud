-- SSC gap 7 and gap 1 · OAuth at the auth host: remote MCP clients and the console (decision 029).

-- Clients that registered themselves (RFC 7591): public clients only, so there is no secret to
-- keep. Global, like org_index: a client registers before anyone has signed in, so it belongs to
-- no org. It holds a client id, the name the client gave and where codes may be sent, nothing
-- about any person. A client unused for 30 days is deleted (identity.jobs).
CREATE TABLE ssc.oauth_client (
  client_id     text PRIMARY KEY CHECK (client_id ~ '^[A-Za-z0-9_-]{22}$'),
  client_name   text NOT NULL CHECK (length(client_name) BETWEEN 1 AND 100),
  redirect_uris text[] NOT NULL CHECK (cardinality(redirect_uris) BETWEEN 1 AND 10),
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz
);
CREATE INDEX oauth_client_last_use ON ssc.oauth_client (coalesce(last_used_at, created_at));

GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.oauth_client TO ssc_app;

-- An authorization code (RFC 6749 with PKCE, RFC 7636). Only its SHA-256 is kept. It works once,
-- for a minute, for the client, redirect URI, PKCE challenge and resource it was issued for. A
-- second use revokes the session it opened.
CREATE TABLE ssc.oauth_code (
  id             text PRIMARY KEY CHECK (id ~ '^oac_[a-z0-9]{20}$'),
  org_id         text NOT NULL,
  code_hash      bytea NOT NULL UNIQUE CHECK (length(code_hash) = 32),
  client_id      text NOT NULL CHECK (length(client_id) BETWEEN 1 AND 64),
  redirect_uri   text NOT NULL CHECK (length(redirect_uri) BETWEEN 1 AND 2000),
  code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{43}$'),
  resource       text NOT NULL CHECK (length(resource) BETWEEN 1 AND 300),
  session_id     text NOT NULL,
  user_id        text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL,
  used_at        timestamptz,
  UNIQUE (org_id, id),
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '5 minutes'),
  FOREIGN KEY (org_id, session_id) REFERENCES ssc.auth_session (org_id, id),
  FOREIGN KEY (org_id, user_id) REFERENCES ssc.user_account (org_id, id)
);
CREATE INDEX oauth_code_expiry ON ssc.oauth_code (org_id, expires_at);

ALTER TABLE ssc.oauth_code ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.oauth_code FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.oauth_code USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.oauth_code TO ssc_app;

-- A console sign-in is its own kind. token_audience is the audience of the session's access
-- tokens, kept for refresh: NULL is the API's user audience, an MCP client's is the MCP resource.
-- 'code_reuse': an authorization code was presented twice.
ALTER TABLE ssc.auth_session
  DROP CONSTRAINT auth_session_kind_check,
  ADD CONSTRAINT auth_session_kind_check CHECK (kind IN ('browser', 'cli', 'console')),
  DROP CONSTRAINT auth_session_revoke_reason_check,
  ADD CONSTRAINT auth_session_revoke_reason_check CHECK (revoke_reason IN (
    'logout', 'user_deactivated', 'refresh_reuse', 'operator', 'code_reuse')),
  ADD COLUMN token_audience text CHECK (length(token_audience) BETWEEN 1 AND 300);

-- Which org signs in through a WorkOS organisation, for the work-email step of /authorize, which
-- runs before any org is known. directory_connection is org-scoped and every role, its owner too,
-- is subject to row-level security, so the function binds each org in turn and puts the caller's
-- bind back before it returns. (A `SET ssc.org` clause would do that too, but a non-superuser may
-- not name a placeholder setting in one, and the migrator is not a superuser.) An error inside
-- aborts the caller's transaction or savepoint, which undoes the binds as well. Only active
-- connections count. O(orgs) lookups: fine for the pilot's hundreds of orgs; the decision names
-- the upgrade (a global route table written with the connection). SECURITY DEFINER so the app
-- role never binds orgs it was not asked about; executable by the app role only.
CREATE FUNCTION ssc.org_for_workos_organization(workos_org text) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, ssc
AS $$
DECLARE
  prior text := coalesce(current_setting('ssc.org', true), '');
  o text;
  found text;
BEGIN
  FOR o IN SELECT org_id FROM ssc.org_index ORDER BY org_id LOOP
    PERFORM set_config('ssc.org', o, true);
    SELECT c.org_id INTO found FROM ssc.directory_connection c
     WHERE c.workos_organization_id = workos_org AND c.state = 'active';
    EXIT WHEN found IS NOT NULL;
  END LOOP;
  PERFORM set_config('ssc.org', prior, true);
  RETURN found;
END;
$$;
REVOKE ALL ON FUNCTION ssc.org_for_workos_organization(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ssc.org_for_workos_organization(text) TO ssc_app;

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
    'github.installation_bound', 'repo.connected', 'repo.disconnected'));
