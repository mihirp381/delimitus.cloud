-- CD-1 · An org's apps suffix: its app hosts are <slug>.<suffix> and <slug>--preview.<suffix>.
-- NULL is the default, <cell label>.<apps domain>; a delegated subdomain sets it. 203 is
-- ssc_contracts.snapshot.MAX_SUFFIX: the longest first label (49) and a dot fit beside it in 253.
ALTER TABLE ssc.org
  ADD COLUMN apps_suffix text UNIQUE CHECK (
    apps_suffix ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?){2,}$'
    AND length(apps_suffix) <= 203);

-- Suffixes an org used before, recognised until `until` (CD-4 moves the current one here).
CREATE TABLE ssc.org_suffix_retired (
  org_id text NOT NULL REFERENCES ssc.org (id),
  suffix text NOT NULL UNIQUE CHECK (
    suffix ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?){2,}$'
    AND length(suffix) <= 203),
  until  timestamptz NOT NULL,
  PRIMARY KEY (org_id, suffix)
);

CREATE TRIGGER org_suffix_retired_refuse_truncate
  BEFORE TRUNCATE ON ssc.org_suffix_retired
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.org_suffix_retired ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.org_suffix_retired FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.org_suffix_retired USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, DELETE ON ssc.org_suffix_retired TO ssc_app;
