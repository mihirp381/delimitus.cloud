ALTER TABLE ssc.metrics_event
  ADD COLUMN environment_id text,
  ADD COLUMN dedup_key text,
  ADD CONSTRAINT metrics_event_environment_id_check
    CHECK (environment_id ~ '^env_[a-z0-9]{20}$'),
  ADD CONSTRAINT metrics_event_dedup_key_check CHECK (dedup_key ~ '^[a-z0-9_:]{1,64}$');

ALTER TABLE ssc.metrics_event DROP CONSTRAINT metrics_event_kind_check;
ALTER TABLE ssc.metrics_event ADD CONSTRAINT metrics_event_kind_check CHECK (kind IN (
  'first_url', 'deploy', 'share', 'app_opened', 'data_query', 'database_use', 'timer_run',
  'usage_hour', 'cold_start', 'fixed_resource'));

CREATE UNIQUE INDEX metrics_event_once
  ON ssc.metrics_event (org_id, kind, dedup_key)
  WHERE dedup_key IS NOT NULL;

CREATE INDEX metrics_event_environment_at
  ON ssc.metrics_event (org_id, environment_id, at)
  WHERE environment_id IS NOT NULL;

CREATE TABLE ssc.usage_collection (
  org_id          text PRIMARY KEY REFERENCES ssc.org (id),
  collected_until timestamptz NOT NULL
                  CHECK (extract(epoch FROM collected_until) % 3600 = 0),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER usage_collection_refuse_truncate
  BEFORE TRUNCATE ON ssc.usage_collection
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.usage_collection ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.usage_collection FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.usage_collection USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.usage_collection TO ssc_app;
