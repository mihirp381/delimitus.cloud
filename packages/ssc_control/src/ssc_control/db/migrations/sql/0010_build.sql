-- SSC-016 (B4): ssc.build, one row per build of a stored bundle for one environment, and
-- deployment.failure_code. A build is queued, then running, then succeeded with the release it
-- created (at most one) or failed with a reason code. Only one build of a bundle per environment
-- is in flight at a time.

CREATE TABLE ssc.build (
  id              text PRIMARY KEY CHECK (id ~ '^bld_[a-z0-9]{20}$'),
  org_id          text NOT NULL,
  app_id          text NOT NULL,
  environment_id  text NOT NULL,
  bundle_id       text NOT NULL,
  state           text NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
  failure_code    text CHECK (failure_code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
  driver_ref      text CHECK (length(driver_ref) BETWEEN 1 AND 512),
  release_id      text,
  actor_kind      text NOT NULL CHECK (actor_kind IN ('user', 'workload', 'schedule', 'operator', 'integration')),
  actor_id        text NOT NULL,
  actor_via_agent boolean NOT NULL DEFAULT false,
  actor_client_id text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  CONSTRAINT build_release_check CHECK ((state = 'succeeded') = (release_id IS NOT NULL)),
  CONSTRAINT build_failure_check CHECK ((state = 'failed') = (failure_code IS NOT NULL)),
  CONSTRAINT build_finished_check CHECK ((state IN ('queued', 'running')) = (finished_at IS NULL)),
  CONSTRAINT build_started_check CHECK ((state = 'queued') = (started_at IS NULL)),
  UNIQUE (org_id, id),
  UNIQUE (org_id, release_id),
  FOREIGN KEY (org_id, app_id, environment_id) REFERENCES ssc.environment (org_id, app_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, app_id, bundle_id)      REFERENCES ssc.bundle (org_id, app_id, id),
  FOREIGN KEY (org_id, app_id, release_id)     REFERENCES ssc.release (org_id, app_id, id)
);

CREATE UNIQUE INDEX build_one_in_flight
  ON ssc.build (org_id, environment_id, bundle_id)
  WHERE state IN ('queued', 'running');
CREATE INDEX build_environment_idx ON ssc.build (org_id, environment_id, created_at DESC);

ALTER TABLE ssc.build ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.build FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.build
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.build TO ssc_app;

-- Why a deployment failed: a reason code, set only on failed rows. Existing rows are never
-- failed with a code, so the CHECK holds for every row already there.
ALTER TABLE ssc.deployment
  ADD COLUMN failure_code text CHECK (failure_code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
  ADD CONSTRAINT deployment_failure_check CHECK (failure_code IS NULL OR state = 'failed');
