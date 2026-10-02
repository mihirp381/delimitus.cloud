-- SSC-019 · Company login and directory sync (decision 024).

-- Directory sync is never blocked: deactivating the org's last active admin is allowed, and an
-- SSC operator restores an admin. Demoting or deleting the last active admin is still refused.
CREATE OR REPLACE FUNCTION ssc.refuse_last_org_admin() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  remaining integer;
BEGIN
  IF OLD.role <> 'admin' OR OLD.status <> 'active' THEN
    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.role = 'admin' OR NEW.status = 'deactivated') THEN
    RETURN NEW;
  END IF;
  PERFORM 1 FROM ssc.org WHERE id = OLD.org_id FOR UPDATE;
  SELECT count(*) INTO remaining
    FROM ssc.user_account
   WHERE org_id = OLD.org_id AND id <> OLD.id AND role = 'admin' AND status = 'active';
  IF remaining = 0 THEN
    RAISE EXCEPTION USING
      ERRCODE = 'SC002',
      MESSAGE = format('org %s must keep at least one active admin', OLD.org_id);
  END IF;
  RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

-- Set on every revocation: a gateway session issued before it is refused, even after the person
-- is reactivated. Published in the access snapshot.
ALTER TABLE ssc.user_account ADD COLUMN sessions_not_before timestamptz;

-- 'admin': an org admin tied an SSO login to this person (the Unlinked logins list).
ALTER TABLE ssc.identity_link
  ADD COLUMN source text NOT NULL DEFAULT 'directory' CHECK (source IN ('directory', 'admin'));

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
    'directory.connected', 'directory.frozen', 'identity.linked'));

-- The org's WorkOS organisation, its directory and the SSO connections allowed to sign in.
-- One per org. Ids are unique across orgs: one WorkOS organisation never serves two SSC orgs.
CREATE TABLE ssc.directory_connection (
  id                     text PRIMARY KEY CHECK (id ~ '^dcn_[a-z0-9]{20}$'),
  org_id                 text NOT NULL REFERENCES ssc.org (id),
  workos_organization_id text NOT NULL UNIQUE CHECK (workos_organization_id ~ '^org_[0-9A-Za-z]{1,64}$'),
  workos_directory_id    text NOT NULL UNIQUE CHECK (workos_directory_id ~ '^directory_[0-9A-Za-z]{1,64}$'),
  sso_connection_ids     text[] NOT NULL CHECK (cardinality(sso_connection_ids) BETWEEN 1 AND 4),
  join_rule              text NOT NULL CHECK (join_rule IN ('idp_id', 'email')),
  admin_group_ref        text CHECK (length(admin_group_ref) BETWEEN 1 AND 300),
  state                  text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'frozen')),
  frozen_reason          text CHECK (frozen_reason IN ('directory_deleted', 'operator')),
  event_cursor           text CHECK (length(event_cursor) <= 200),
  last_sync_ok_at        timestamptz,
  last_full_sync_at      timestamptz,
  last_error             text CHECK (length(last_error) <= 200),
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id),
  CHECK ((state = 'frozen') = (frozen_reason IS NOT NULL))
);

-- A sign-in at the auth host: a browser's (gateway login codes hang off it) or the command
-- line's (refresh tokens hang off it). Twelve hours at most, never extended.
CREATE TABLE ssc.auth_session (
  id            text PRIMARY KEY CHECK (id ~ '^ses_[a-z0-9]{20}$'),
  org_id        text NOT NULL,
  user_id       text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('browser', 'cli')),
  connection_id text NOT NULL CHECK (length(connection_id) <= 100),
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  revoked_at    timestamptz,
  revoke_reason text CHECK (revoke_reason IN (
                  'logout', 'user_deactivated', 'refresh_reuse', 'operator')),
  UNIQUE (org_id, id),
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '12 hours'),
  CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL)),
  FOREIGN KEY (org_id, user_id) REFERENCES ssc.user_account (org_id, id)
);
CREATE INDEX auth_session_live ON ssc.auth_session (org_id, user_id) WHERE revoked_at IS NULL;

-- The one-time code the auth host hands an app host. Only its SHA-256 is kept. It works once,
-- for one host, for the browser holding the gateway's login nonce (binding_hash), for a minute.
CREATE TABLE ssc.login_code (
  id           text PRIMARY KEY CHECK (id ~ '^lgc_[a-z0-9]{20}$'),
  org_id       text NOT NULL,
  session_id   text NOT NULL,
  code_hash    bytea NOT NULL UNIQUE CHECK (length(code_hash) = 32),
  binding_hash bytea NOT NULL CHECK (length(binding_hash) = 32),
  host         text NOT NULL CHECK (length(host) BETWEEN 1 AND 253),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz,
  UNIQUE (org_id, id),
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '5 minutes'),
  FOREIGN KEY (org_id, session_id) REFERENCES ssc.auth_session (org_id, id)
);
CREATE INDEX login_code_expiry ON ssc.login_code (org_id, expires_at);

-- Command-line refresh tokens, hashed. Each is used once; a second use revokes the session.
CREATE TABLE ssc.refresh_token (
  id         text PRIMARY KEY CHECK (id ~ '^rft_[a-z0-9]{20}$'),
  org_id     text NOT NULL,
  session_id text NOT NULL,
  token_hash bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
  created_at timestamptz NOT NULL DEFAULT now(),
  used_at    timestamptz,
  UNIQUE (org_id, id),
  FOREIGN KEY (org_id, session_id) REFERENCES ssc.auth_session (org_id, id)
);
CREATE INDEX refresh_token_session ON ssc.refresh_token (org_id, session_id);

-- RFC 8628 device authorisation for `ssc login`. The device code is hashed; the user code is
-- shown to the person and unique among the org's pending grants.
CREATE TABLE ssc.device_grant (
  id               text PRIMARY KEY CHECK (id ~ '^dvg_[a-z0-9]{20}$'),
  org_id           text NOT NULL,
  device_code_hash bytea NOT NULL UNIQUE CHECK (length(device_code_hash) = 32),
  user_code        text NOT NULL CHECK (user_code ~ '^[BCDFGHJKLMNPQRSTVWXZ]{8}$'),
  state            text NOT NULL DEFAULT 'pending' CHECK (state IN (
                     'pending', 'approved', 'denied', 'consumed')),
  session_id       text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  last_polled_at   timestamptz,
  UNIQUE (org_id, id),
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '15 minutes'),
  CHECK ((state IN ('approved', 'consumed')) = (session_id IS NOT NULL)),
  FOREIGN KEY (org_id, session_id) REFERENCES ssc.auth_session (org_id, id)
);
CREATE UNIQUE INDEX device_grant_pending_code ON ssc.device_grant (org_id, user_code)
  WHERE state = 'pending';

-- SSO logins that matched no one (or more than one active person) in the directory. They give
-- no access; an org admin may link one to a person, which is stored and audited.
CREATE TABLE ssc.unlinked_login (
  id             text PRIMARY KEY CHECK (id ~ '^ulg_[a-z0-9]{20}$'),
  org_id         text NOT NULL,
  connection_id  text NOT NULL CHECK (length(connection_id) <= 100),
  subject        text NOT NULL CHECK (length(subject) BETWEEN 1 AND 300),
  email          text NOT NULL CHECK (length(email) <= 320),
  reason         text NOT NULL CHECK (reason IN ('no_match', 'ambiguous_email')),
  attempts       integer NOT NULL DEFAULT 1 CHECK (attempts >= 1),
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  linked_user_id text,
  linked_at      timestamptz,
  UNIQUE (org_id, id),
  UNIQUE (org_id, connection_id, subject),
  CHECK ((linked_user_id IS NULL) = (linked_at IS NULL)),
  FOREIGN KEY (org_id, linked_user_id) REFERENCES ssc.user_account (org_id, id)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['directory_connection', 'auth_session', 'login_code', 'refresh_token',
                           'device_grant', 'unlinked_login'] LOOP
    EXECUTE format('ALTER TABLE ssc.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE ssc.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY org_isolation ON ssc.%I USING (org_id = ssc.current_org()) '
                   'WITH CHECK (org_id = ssc.current_org())', t);
  END LOOP;
END;
$$;

GRANT SELECT, INSERT, UPDATE         ON ssc.directory_connection TO ssc_app;
GRANT SELECT, INSERT, UPDATE         ON ssc.auth_session         TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.login_code           TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.refresh_token        TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.device_grant         TO ssc_app;
GRANT SELECT, INSERT, UPDATE         ON ssc.unlinked_login       TO ssc_app;
