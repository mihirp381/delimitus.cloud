-- SSC-010 · Control database: bookkeeping tables with customer separation enforced by Postgres.
--
-- Runs as the migrator role (`ssc_migrate`), which therefore owns every object here.
-- The application connects as `ssc_app`: a non-owner role without BYPASSRLS that is granted
-- exactly the privileges listed at the bottom of this file and nothing on the migration ledger.
--
-- Rules this file enforces (see ../../PLPGSQL.md, ../../PII.md and docs/decisions 009):
--   * every table carries org_id; foreign keys are (org_id, id) pairs so a row can never point
--     across customers;
--   * row-level security is ENABLED and FORCED on every table, with one policy per table that
--     compares org_id to ssc.current_org();
--   * ssc.current_org() RAISES with SQLSTATE SC001 when no org is bound. It never returns NULL,
--     because a NULL makes every policy false and every query return zero rows with HTTP 200;
--   * the bind is transaction-scoped: set_config('ssc.org', <org id>, true). The application
--     binds once per unit of work (ssc_control.db.bind); a session-scoped bind would leak into
--     the next borrower of a pooled connection;
--   * PL/pgSQL is a bounded exemption from the Python rule: the functions below are the whole
--     list, and a catalog test refuses any function in schema ssc that is not in PLPGSQL.md.
--
-- Custom SQLSTATE class "SC" (Python mirror: ssc_control.db.errors.SqlState):
--   SC001 no org bound        SC002 last active org admin      SC003 app owner not active
--   SC004 release immutable   SC005 audit row immutable         SC006 truncate refused
--   SC007 schedule deleted    (each RAISE below names its code)

CREATE SCHEMA IF NOT EXISTS ssc;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. The fail-loud scope helper
-- ─────────────────────────────────────────────────────────────────────────────

CREATE FUNCTION ssc.current_org() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
  o text;
BEGIN
  o := current_setting('ssc.org', true);
  IF o IS NULL OR o = '' THEN
    RAISE EXCEPTION USING
      ERRCODE = 'SC001',
      MESSAGE = 'no org is bound to this transaction',
      HINT    = 'bind once per unit of work with ssc_control.db.bind.bound_org()';
  END IF;
  RETURN o;
END;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Guard trigger functions (the bounded PL/pgSQL set)
-- ─────────────────────────────────────────────────────────────────────────────

-- An active admin may only stop being one while another active admin remains.
-- The FOR UPDATE on the org row serialises two admins removing each other at once:
-- a read-then-write count is not a check.
CREATE FUNCTION ssc.refuse_last_org_admin() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  remaining integer;
BEGIN
  IF OLD.role <> 'admin' OR OLD.status <> 'active' THEN
    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.role = 'admin' AND NEW.status = 'active' THEN
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

-- An app is created for, or transferred to, an active member of the same org.
-- Deactivating an owner later is allowed (directory sync must never be blocked);
-- the inventory (SSC-025) lists apps whose owner is no longer active for transfer.
CREATE FUNCTION ssc.owner_must_be_active() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.owner_user_id IS NULL THEN
    RETURN NEW;  -- NOT NULL reports that one, with its own SQLSTATE
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM ssc.user_account
     WHERE org_id = NEW.org_id AND id = NEW.owner_user_id AND status = 'active'
  ) THEN
    RAISE EXCEPTION USING
      ERRCODE = 'SC003',
      MESSAGE = format('app owner %s is not an active member of org %s', NEW.owner_user_id, NEW.org_id);
  END IF;
  RETURN NEW;
END;
$$;

-- Rows that may never change once written. TG_ARGV[0] is the SQLSTATE to raise.
CREATE FUNCTION ssc.refuse_row_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION USING
    ERRCODE = TG_ARGV[0],
    MESSAGE = format('%s rows are immutable: %s refused', TG_TABLE_NAME, TG_OP);
END;
$$;

CREATE FUNCTION ssc.refuse_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION USING
    ERRCODE = 'SC006',
    MESSAGE = format('TRUNCATE %s refused', TG_TABLE_NAME);
END;
$$;

-- A deleted schedule is terminal. Un-deleting would resurrect timers nobody expects.
CREATE FUNCTION ssc.schedule_terminal_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'deleted' THEN
    RAISE EXCEPTION USING
      ERRCODE = 'SC007',
      MESSAGE = format('schedule %s is deleted and cannot change', OLD.id);
  END IF;
  RETURN NEW;
END;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Tables
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE ssc.org (
  id           text PRIMARY KEY CHECK (id ~ '^org_[a-z0-9]{20}$'),
  name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  -- Opaque label used in every app host name. Never the customer's name: certificate
  -- logs would otherwise reveal who our customers are. NULL until SSC-013 creates the cell.
  cell_label   text UNIQUE CHECK (cell_label ~ '^[a-z][a-z0-9]{7,15}$'),
  cell_project text UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Named user_account, not user: `user` is a reserved word in Postgres (`SELECT user` returns
-- the session user) and quoting it everywhere is how a rule gets forgotten once.
CREATE TABLE ssc.user_account (
  id             text PRIMARY KEY CHECK (id ~ '^usr_[a-z0-9]{20}$'),
  org_id         text NOT NULL REFERENCES ssc.org (id),
  display_name   text NOT NULL,                 -- PII, display only
  email          text NOT NULL,                 -- PII, display only, never a key (see identity_link)
  role           text NOT NULL CHECK (role IN ('admin', 'member')),
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deactivated')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  deactivated_at timestamptz,
  CHECK ((status = 'deactivated') = (deactivated_at IS NOT NULL)),
  UNIQUE (org_id, id)
);

CREATE TRIGGER refuse_last_org_admin
  BEFORE UPDATE OR DELETE ON ssc.user_account
  FOR EACH ROW EXECUTE FUNCTION ssc.refuse_last_org_admin();

CREATE TABLE ssc.user_group (
  id            text PRIMARY KEY CHECK (id ~ '^grp_[a-z0-9]{20}$'),
  org_id        text NOT NULL REFERENCES ssc.org (id),
  directory_ref text NOT NULL,                  -- the provider's group id: the authorisation key
  display_name  text NOT NULL,                  -- PII (cached group name), rendering only
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, directory_ref)
);

CREATE TABLE ssc.group_member (
  org_id   text NOT NULL,
  group_id text NOT NULL,
  user_id  text NOT NULL,
  PRIMARY KEY (org_id, group_id, user_id),
  FOREIGN KEY (org_id, group_id) REFERENCES ssc.user_group (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, user_id)  REFERENCES ssc.user_account (org_id, id) ON DELETE CASCADE
);
CREATE INDEX group_member_user_idx ON ssc.group_member (org_id, user_id);

-- The join between a login and a person: (issuer, subject), never email.
CREATE TABLE ssc.identity_link (
  id         text PRIMARY KEY CHECK (id ~ '^idl_[a-z0-9]{20}$'),
  org_id     text NOT NULL,
  user_id    text NOT NULL,
  issuer     text NOT NULL,
  subject    text NOT NULL,                     -- PII: the provider's stable id for the person
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, issuer, subject),
  FOREIGN KEY (org_id, user_id) REFERENCES ssc.user_account (org_id, id) ON DELETE CASCADE
);

CREATE TABLE ssc.app (
  id            text PRIMARY KEY CHECK (id ~ '^app_[a-z0-9]{20}$'),
  org_id        text NOT NULL REFERENCES ssc.org (id),
  -- `--` is reserved for the `--<env>` host suffix.
  slug          text NOT NULL CHECK (slug ~ '^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$' AND slug NOT LIKE '%--%'),
  owner_user_id text NOT NULL,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'quarantined')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, slug),
  FOREIGN KEY (org_id, owner_user_id) REFERENCES ssc.user_account (org_id, id)
);

CREATE TRIGGER owner_must_be_active
  BEFORE INSERT OR UPDATE OF owner_user_id ON ssc.app
  FOR EACH ROW EXECUTE FUNCTION ssc.owner_must_be_active();

-- Grants, secrets, schedules and connections attach to the environment, never to a release,
-- so rolling code back never brings an old permission back.
CREATE TABLE ssc.environment (
  id                    text PRIMARY KEY CHECK (id ~ '^env_[a-z0-9]{20}$'),
  org_id                text NOT NULL,
  app_id                text NOT NULL,
  name                  text NOT NULL CHECK (name IN ('prod', 'preview')),
  config_version        bigint NOT NULL DEFAULT 1 CHECK (config_version >= 1),
  grants_version        bigint NOT NULL DEFAULT 1 CHECK (grants_version >= 1),
  current_deployment_id text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, app_id, id),
  UNIQUE (org_id, app_id, name),
  FOREIGN KEY (org_id, app_id) REFERENCES ssc.app (org_id, id) ON DELETE CASCADE
);

CREATE TABLE ssc.release (
  id              text PRIMARY KEY CHECK (id ~ '^rel_[a-z0-9]{20}$'),
  org_id          text NOT NULL,
  app_id          text NOT NULL,
  number          integer NOT NULL CHECK (number >= 1),
  image_digest    text NOT NULL CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
  manifest_digest text NOT NULL CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  source_digest   text NOT NULL CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),
  source_commit   text CHECK (source_commit ~ '^[0-9a-f]{40}$'),
  scan_refs       jsonb NOT NULL DEFAULT '[]'::jsonb,
  actor_kind      text NOT NULL CHECK (actor_kind IN ('user', 'workload', 'schedule', 'operator', 'integration')),
  actor_id        text NOT NULL,
  actor_via_agent boolean NOT NULL DEFAULT false,
  actor_client_id text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, app_id, id),
  UNIQUE (org_id, app_id, number),
  FOREIGN KEY (org_id, app_id) REFERENCES ssc.app (org_id, id)
);

CREATE TRIGGER release_is_immutable
  BEFORE UPDATE OR DELETE ON ssc.release
  FOR EACH ROW EXECUTE FUNCTION ssc.refuse_row_change('SC004');
CREATE TRIGGER release_refuse_truncate
  BEFORE TRUNCATE ON ssc.release
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

-- A deployment runs one release in one environment under that environment's versions at the
-- time. app_id is carried so the (org, app) pair on the release and on the environment must agree.
CREATE TABLE ssc.deployment (
  id              text PRIMARY KEY CHECK (id ~ '^dep_[a-z0-9]{20}$'),
  org_id          text NOT NULL,
  app_id          text NOT NULL,
  environment_id  text NOT NULL,
  release_id      text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('deploy', 'rollback')),
  state           text NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'running', 'healthy', 'failed', 'superseded')),
  config_version  bigint NOT NULL CHECK (config_version >= 1),
  grants_version  bigint NOT NULL CHECK (grants_version >= 1),
  actor_kind      text NOT NULL CHECK (actor_kind IN ('user', 'workload', 'schedule', 'operator', 'integration')),
  actor_id        text NOT NULL,
  actor_via_agent boolean NOT NULL DEFAULT false,
  actor_client_id text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  CHECK ((state IN ('pending', 'running')) = (finished_at IS NULL)),
  UNIQUE (org_id, id),
  FOREIGN KEY (org_id, app_id, environment_id) REFERENCES ssc.environment (org_id, app_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, app_id, release_id)     REFERENCES ssc.release (org_id, app_id, id)
);

-- Only one deployment per environment may be in flight. A rollback pre-empts it by marking it
-- superseded first (service layer), which frees this index.
CREATE UNIQUE INDEX deployment_one_in_flight
  ON ssc.deployment (org_id, environment_id)
  WHERE state IN ('pending', 'running');
CREATE INDEX deployment_environment_idx ON ssc.deployment (org_id, environment_id, started_at DESC);

ALTER TABLE ssc.environment
  ADD FOREIGN KEY (org_id, current_deployment_id) REFERENCES ssc.deployment (org_id, id);

CREATE TABLE ssc.app_grant (
  id                 text PRIMARY KEY CHECK (id ~ '^gnt_[a-z0-9]{20}$'),
  org_id             text NOT NULL,
  environment_id     text NOT NULL,
  role               text NOT NULL CHECK (role IN ('builder', 'user')),
  subject_kind       text NOT NULL CHECK (subject_kind IN ('user', 'group', 'org')),
  user_id            text,
  group_id           text,
  granted_by_user_id text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (
       (subject_kind = 'user'  AND user_id IS NOT NULL AND group_id IS NULL)
    OR (subject_kind = 'group' AND group_id IS NOT NULL AND user_id IS NULL)
    OR (subject_kind = 'org'   AND user_id IS NULL AND group_id IS NULL)
  ),
  UNIQUE (org_id, id),
  FOREIGN KEY (org_id, environment_id)     REFERENCES ssc.environment (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, user_id)            REFERENCES ssc.user_account (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, group_id)           REFERENCES ssc.user_group (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, granted_by_user_id) REFERENCES ssc.user_account (org_id, id)
);
CREATE UNIQUE INDEX app_grant_one_per_subject
  ON ssc.app_grant (org_id, environment_id, subject_kind, coalesce(user_id, ''), coalesce(group_id, ''));

-- A reference to a version in the cell's Secret Manager. There is no value column, and a
-- catalog test refuses one.
CREATE TABLE ssc.secret_ref (
  id             text PRIMARY KEY CHECK (id ~ '^sec_[a-z0-9]{20}$'),
  org_id         text NOT NULL,
  environment_id text NOT NULL,
  name           text NOT NULL CHECK (name ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  secret_version text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, environment_id, name),
  FOREIGN KEY (org_id, environment_id) REFERENCES ssc.environment (org_id, id) ON DELETE CASCADE
);

CREATE TABLE ssc.schedule (
  id             text PRIMARY KEY CHECK (id ~ '^sch_[a-z0-9]{20}$'),
  org_id         text NOT NULL,
  environment_id text NOT NULL,
  name           text NOT NULL CHECK (name ~ '^[a-z][a-z0-9-]{0,62}$'),
  cron           text NOT NULL,
  timezone       text NOT NULL DEFAULT 'UTC',
  state          text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'paused', 'deleted')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, environment_id, name),
  FOREIGN KEY (org_id, environment_id) REFERENCES ssc.environment (org_id, id) ON DELETE CASCADE
);

CREATE TRIGGER schedule_terminal_state
  BEFORE UPDATE ON ssc.schedule
  FOR EACH ROW EXECUTE FUNCTION ssc.schedule_terminal_state();

CREATE TABLE ssc.connection (
  id             text PRIMARY KEY CHECK (id ~ '^con_[a-z0-9]{20}$'),
  org_id         text NOT NULL REFERENCES ssc.org (id),
  name           text NOT NULL CHECK (name ~ '^[a-z][a-z0-9-]{0,62}$'),
  kind           text NOT NULL CHECK (kind IN ('postgres')),
  classification text NOT NULL CHECK (classification IN ('internal', 'confidential', 'restricted')),
  host           text NOT NULL,
  port           integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
  database_name  text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (org_id, name)
);

-- Only a person can approve, never the requester, and never through an agent. The
-- decided_via_agent flag is written by the service from the credential; the CHECK makes a
-- true value unstorable, so the rule holds even against a service bug.
CREATE TABLE ssc.approval_request (
  id                   text PRIMARY KEY CHECK (id ~ '^apr_[a-z0-9]{20}$'),
  org_id               text NOT NULL,
  kind                 text NOT NULL,
  payload              jsonb NOT NULL DEFAULT '{}'::jsonb,  -- what is being asked for
  requested_by_user_id text NOT NULL,
  requested_via_agent  boolean NOT NULL DEFAULT false,
  state                text NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'approved', 'denied', 'cancelled')),
  decided_by_user_id   text,
  decided_at           timestamptz,
  decided_via_agent    boolean NOT NULL DEFAULT false CHECK (decided_via_agent = false),
  created_at           timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'pending') = (decided_by_user_id IS NULL)),
  CHECK ((state = 'pending') = (decided_at IS NULL)),
  CHECK (state <> 'approved' OR decided_by_user_id IS DISTINCT FROM requested_by_user_id),
  UNIQUE (org_id, id),
  FOREIGN KEY (org_id, requested_by_user_id) REFERENCES ssc.user_account (org_id, id),
  FOREIGN KEY (org_id, decided_by_user_id)   REFERENCES ssc.user_account (org_id, id)
);

CREATE TABLE ssc.policy_decision (
  id               text PRIMARY KEY CHECK (id ~ '^pol_[a-z0-9]{20}$'),
  org_id           text NOT NULL REFERENCES ssc.org (id),
  at               timestamptz NOT NULL DEFAULT now(),
  principal_kind   text NOT NULL CHECK (principal_kind IN ('user', 'group', 'workload', 'schedule', 'integration', 'operator')),
  principal_id     text NOT NULL,
  action           text NOT NULL,
  target_kind      text NOT NULL,
  target_id        text NOT NULL,
  outcome          text NOT NULL CHECK (outcome IN ('allow', 'deny')),
  reason           text NOT NULL,
  snapshot_version bigint,
  inputs           jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (org_id, id)
);

-- Tamper-evident log. Appends are serialised per org by locking audit_head (SSC-012 owns the
-- Python that does this); the constraints here make a fork or a gap impossible to store.
CREATE TABLE ssc.audit_event (
  org_id             text NOT NULL REFERENCES ssc.org (id),
  seq                bigint NOT NULL CHECK (seq >= 1),
  at                 timestamptz NOT NULL DEFAULT now(),
  action             text NOT NULL CHECK (action IN (
                       'org.created', 'user.created', 'user.deactivated', 'user.reactivated',
                       'group.synced',
                       'app.created', 'app.owner_transferred', 'app.disabled', 'app.quarantined',
                       'app.enabled', 'app.deleted',
                       'login.succeeded', 'login.failed', 'token.issued', 'token.revoked',
                       'secret.bound', 'secret.rotated', 'secret.removed',
                       'grant.added', 'grant.removed',
                       'deploy.started', 'deploy.finished', 'deploy.failed',
                       'rollback.started', 'rollback.finished', 'rollback.failed',
                       'kill_switch.step',
                       'approval.requested', 'approval.decided',
                       'schedule.created', 'schedule.paused', 'schedule.resumed', 'schedule.deleted',
                       'connection.created', 'connection.removed',
                       'operator.access')),
  actor_kind         text NOT NULL CHECK (actor_kind IN ('user', 'workload', 'schedule', 'operator', 'integration')),
  actor_id           text NOT NULL,
  actor_via_agent    boolean NOT NULL DEFAULT false,
  actor_client_id    text,
  actor_ip           inet,                     -- PII
  target_kind        text NOT NULL,
  target_id          text NOT NULL,
  before             jsonb,                    -- from allowlisted views only
  after              jsonb,
  policy_decision_id text,
  canonical          bytea NOT NULL,
  prev_hash          bytea NOT NULL CHECK (octet_length(prev_hash) = 32),
  hash               bytea NOT NULL CHECK (octet_length(hash) = 32),
  PRIMARY KEY (org_id, seq),
  UNIQUE (org_id, prev_hash),
  UNIQUE (org_id, hash),
  FOREIGN KEY (org_id, policy_decision_id) REFERENCES ssc.policy_decision (org_id, id)
);
CREATE INDEX audit_event_at_idx     ON ssc.audit_event (org_id, at);
CREATE INDEX audit_event_actor_idx  ON ssc.audit_event (org_id, actor_kind, actor_id, at);
CREATE INDEX audit_event_target_idx ON ssc.audit_event (org_id, target_kind, target_id, at);
CREATE INDEX audit_event_action_idx ON ssc.audit_event (org_id, action, at);

CREATE TRIGGER audit_event_append_only
  BEFORE UPDATE OR DELETE ON ssc.audit_event
  FOR EACH ROW EXECUTE FUNCTION ssc.refuse_row_change('SC005');
CREATE TRIGGER audit_event_refuse_truncate
  BEFORE TRUNCATE ON ssc.audit_event
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

CREATE TABLE ssc.audit_head (
  org_id text PRIMARY KEY REFERENCES ssc.org (id),
  seq    bigint NOT NULL DEFAULT 0 CHECK (seq >= 0),
  hash   bytea NOT NULL CHECK (octet_length(hash) = 32)
);
CREATE TRIGGER audit_head_refuse_truncate
  BEFORE TRUNCATE ON ssc.audit_head
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

-- Product metrics. End users appear only as a keyed pseudonym, never as an id or an email.
CREATE TABLE ssc.metrics_event (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  org_id      text NOT NULL REFERENCES ssc.org (id),
  at          timestamptz NOT NULL DEFAULT now(),
  kind        text NOT NULL CHECK (kind IN (
                'first_url', 'deploy', 'share', 'app_opened', 'data_query', 'database_use', 'timer_run')),
  pseudonym   text,
  app_id      text,
  source_tool text,
  properties  jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX metrics_event_org_at_idx ON ssc.metrics_event (org_id, at);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Row-level security: enabled and FORCED on every table
-- ─────────────────────────────────────────────────────────────────────────────
-- ENABLE alone exempts the table owner. The application is not the owner, but FORCE costs
-- nothing and removes the argument. The catalog test in tests/test_control_db.py fails when a
-- table in schema ssc is missing from this list.

DO $$
DECLARE
  t text;
  tables text[] := ARRAY[
    'user_account', 'user_group', 'group_member', 'identity_link', 'app', 'environment',
    'release', 'deployment', 'app_grant', 'secret_ref', 'schedule', 'connection',
    'approval_request', 'policy_decision', 'audit_event', 'audit_head', 'metrics_event'
  ];
BEGIN
  FOREACH t IN ARRAY tables LOOP
    EXECUTE format('ALTER TABLE ssc.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE ssc.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY org_isolation ON ssc.%I '
      'USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org())', t);
  END LOOP;
END;
$$;

ALTER TABLE ssc.org ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.org FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.org
  USING (id = ssc.current_org()) WITH CHECK (id = ssc.current_org());

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Privileges for the application role. Explicit per table; no ON ALL TABLES.
-- ─────────────────────────────────────────────────────────────────────────────
-- The migration ledger (ssc.alembic_version) gets nothing. The catalog test compares the
-- privileges below with ssc_control.db.catalog.APP_ROLE_PRIVILEGES, so a new table without a
-- written privilege decision fails CI.

GRANT USAGE ON SCHEMA ssc TO ssc_app;

GRANT SELECT, INSERT, UPDATE         ON ssc.org              TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.user_account     TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.user_group       TO ssc_app;
GRANT SELECT, INSERT,         DELETE ON ssc.group_member     TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.identity_link    TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.app              TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.environment      TO ssc_app;
GRANT SELECT, INSERT                 ON ssc.release          TO ssc_app;
GRANT SELECT, INSERT, UPDATE         ON ssc.deployment       TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.app_grant        TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.secret_ref       TO ssc_app;
GRANT SELECT, INSERT, UPDATE         ON ssc.schedule         TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ssc.connection       TO ssc_app;
GRANT SELECT, INSERT, UPDATE         ON ssc.approval_request TO ssc_app;
GRANT SELECT, INSERT                 ON ssc.policy_decision  TO ssc_app;
GRANT SELECT, INSERT                 ON ssc.audit_event      TO ssc_app;
GRANT SELECT, INSERT, UPDATE         ON ssc.audit_head       TO ssc_app;
GRANT SELECT, INSERT                 ON ssc.metrics_event    TO ssc_app;
GRANT USAGE, SELECT ON SEQUENCE ssc.metrics_event_id_seq TO ssc_app;
