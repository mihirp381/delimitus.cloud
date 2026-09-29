-- SSC-017 (B2): queue privileges and ssc.org_index; runs after the vendored schema.

-- Queue privileges for ssc_app, explicit per table (db/README rule 5).
GRANT USAGE ON SCHEMA procrastinate TO ssc_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON procrastinate.procrastinate_jobs            TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON procrastinate.procrastinate_workers         TO ssc_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON procrastinate.procrastinate_periodic_defers TO ssc_app;
GRANT SELECT, INSERT                 ON procrastinate.procrastinate_events          TO ssc_app;
GRANT USAGE ON SEQUENCE procrastinate.procrastinate_jobs_id_seq            TO ssc_app;
GRANT USAGE ON SEQUENCE procrastinate.procrastinate_periodic_defers_id_seq TO ssc_app;
GRANT USAGE ON SEQUENCE procrastinate.procrastinate_events_id_seq          TO ssc_app;

-- The single unscoped table: org ids only, no RLS (decision 009 amendment, db/README rule 14).
CREATE TABLE ssc.org_index (
  org_id     text PRIMARY KEY CHECK (org_id ~ '^org_[a-z0-9]{20}$') REFERENCES ssc.org (id),
  created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON ssc.org_index TO ssc_app;

-- Back-fill: owner lifts FORCE on ssc.org for one statement.
ALTER TABLE ssc.org NO FORCE ROW LEVEL SECURITY;
INSERT INTO ssc.org_index (org_id, created_at) SELECT id, created_at FROM ssc.org;
ALTER TABLE ssc.org FORCE ROW LEVEL SECURITY;
