-- SSC-028 · Metrics events: the shapes the recorder writes, enforced by the database too.
--
-- A pseudonym is 32 hex characters of a keyed HMAC, never an id; a source tool is the normalised
-- tool name; properties are one flat object that carries no user id and no email address.
-- metrics_event has had no writer since 0001, so every existing row passes. CHECK validation scans
-- the table without row-level security, so FORCE stays on.

ALTER TABLE ssc.metrics_event
  ADD CONSTRAINT metrics_event_pseudonym_check CHECK (pseudonym ~ '^[0-9a-f]{32}$'),
  ADD CONSTRAINT metrics_event_source_tool_check CHECK (source_tool ~ '^[a-z0-9._-]{1,40}$'),
  ADD CONSTRAINT metrics_event_app_id_check CHECK (app_id ~ '^app_[a-z0-9]{20}$'),
  ADD CONSTRAINT metrics_event_properties_check CHECK (
    jsonb_typeof(properties) = 'object'
    AND properties::text !~ 'usr_[a-z0-9]{20}'
    AND properties::text !~ '[^@\s"]+@[^@\s"]+\.[^@\s"]+');
