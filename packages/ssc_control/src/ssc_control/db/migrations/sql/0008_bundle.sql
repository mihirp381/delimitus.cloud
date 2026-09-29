-- SSC-014 (B3): ssc.bundle, one row per uploaded source bundle, unique per (org, app, digest).
-- 'pending' from the upload request until complete has checked the stored object, then 'stored'
-- with the manifest the server read from the bundle itself. A release built from a bundle carries
-- source_digest = digest (decision 015).

CREATE TABLE ssc.bundle (
  id              text PRIMARY KEY CHECK (id ~ '^bdl_[a-z0-9]{20}$'),
  org_id          text NOT NULL,
  app_id          text NOT NULL,
  digest          text NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
  size_bytes      bigint NOT NULL CHECK (size_bytes > 0),
  state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'stored')),
  manifest        jsonb CHECK (jsonb_typeof(manifest) = 'object'),
  manifest_digest text CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  source_commit   text CHECK (source_commit ~ '^[0-9a-f]{40}$'),
  file_count      integer CHECK (file_count >= 0),
  actor_kind      text NOT NULL CHECK (actor_kind IN ('user', 'workload', 'schedule', 'operator', 'integration')),
  actor_id        text NOT NULL,
  actor_via_agent boolean NOT NULL DEFAULT false,
  actor_client_id text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  stored_at       timestamptz,
  CONSTRAINT bundle_stored_check CHECK (
    (state = 'pending' AND stored_at IS NULL AND manifest IS NULL
     AND manifest_digest IS NULL AND file_count IS NULL)
    OR (state = 'stored' AND stored_at IS NOT NULL AND manifest IS NOT NULL
        AND manifest_digest IS NOT NULL AND file_count IS NOT NULL)
  ),
  UNIQUE (org_id, id),
  UNIQUE (org_id, app_id, id),
  UNIQUE (org_id, app_id, digest),
  FOREIGN KEY (org_id, app_id) REFERENCES ssc.app (org_id, id)
);

ALTER TABLE ssc.bundle ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.bundle FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.bundle
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.bundle TO ssc_app;
