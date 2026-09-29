-- SSC-021 · Published access snapshots and each org's cell acknowledgement (decision 019).

CREATE TABLE ssc.access_snapshot (
  org_id      text NOT NULL REFERENCES ssc.org (id),
  version     bigint NOT NULL CHECK (version >= 1),
  digest      text NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
  object_key  text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 512),
  compiled_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, version)
);

CREATE TABLE ssc.snapshot_ack (
  org_id     text PRIMARY KEY REFERENCES ssc.org (id),
  cell_label text NOT NULL CHECK (cell_label ~ '^[a-z][a-z0-9]{7,15}$'),
  version    bigint NOT NULL,
  acked_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, version) REFERENCES ssc.access_snapshot (org_id, version)
);

ALTER TABLE ssc.access_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.access_snapshot FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.access_snapshot
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

ALTER TABLE ssc.snapshot_ack ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.snapshot_ack FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.snapshot_ack
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT         ON ssc.access_snapshot TO ssc_app;
GRANT SELECT, INSERT, UPDATE ON ssc.snapshot_ack    TO ssc_app;
