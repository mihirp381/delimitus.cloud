-- Ticket 14 · Who may link a connection to a preview environment without a person deciding:
-- {"who": "approval"}, {"who": "builders"} or {"who": "group", "group_id": "grp_..."}. Rows from
-- before keep approval.
ALTER TABLE ssc.connection NO FORCE ROW LEVEL SECURITY;

ALTER TABLE ssc.connection
  ADD COLUMN preview_access jsonb NOT NULL DEFAULT '{"who": "approval"}'::jsonb
    CONSTRAINT connection_preview_access_check CHECK (jsonb_typeof(preview_access) = 'object');

ALTER TABLE ssc.connection FORCE ROW LEVEL SECURITY;
