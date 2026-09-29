-- SSC-028 · metrics_event shape checks (reasoning in db/PII.md and db/README.md).

ALTER TABLE ssc.metrics_event
  ADD CONSTRAINT metrics_event_pseudonym_check CHECK (pseudonym ~ '^[0-9a-f]{32}$'),
  ADD CONSTRAINT metrics_event_source_tool_check CHECK (source_tool ~ '^[a-z0-9._-]{1,40}$'),
  ADD CONSTRAINT metrics_event_app_id_check CHECK (app_id ~ '^app_[a-z0-9]{20}$'),
  ADD CONSTRAINT metrics_event_properties_check CHECK (
    jsonb_typeof(properties) = 'object'
    AND properties::text !~ 'usr_[a-z0-9]{20}'
    AND properties::text !~ '[^@\s"]+@[^@\s"]+\.[^@\s"]+');
