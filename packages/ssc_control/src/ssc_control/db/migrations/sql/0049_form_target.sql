-- CD-5.2 · Origins an app environment's pages may post forms to, beside its own; the gateway
-- names them in form-action. An org admin replaces the list.
CREATE TABLE ssc.form_target (
  org_id         text NOT NULL,
  environment_id text NOT NULL,
  origin         text NOT NULL CHECK (
    length(origin) <= 261
    AND origin ~ '^https://[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'),
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, environment_id, origin),
  FOREIGN KEY (org_id, environment_id) REFERENCES ssc.environment (org_id, id)
);

CREATE TRIGGER form_target_refuse_truncate
  BEFORE TRUNCATE ON ssc.form_target
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.form_target ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.form_target FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.form_target USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, DELETE ON ssc.form_target TO ssc_app;
