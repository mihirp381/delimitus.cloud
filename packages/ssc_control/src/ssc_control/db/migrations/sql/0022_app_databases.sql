-- SSC-040 · Per-app Postgres.

-- One row per app environment whose database the cell agent made on the org's Cloud SQL
-- instance: where apps reach it and the connection limit of its login role. The password is
-- never here: it lives in the cell's Secret Manager, and ssc.secret_ref holds the versions of
-- DATABASE_URL, PGPASSWORD and DATABASE_CA. host and port are the address the database was made
-- at and stay as they are; rotated_at is the last rotation of the login role's password. A
-- database is never removed through the app role.
CREATE TABLE ssc.app_database (
  org_id           text NOT NULL,
  environment_id   text NOT NULL,
  host             text NOT NULL CHECK (host ~ '^[a-z0-9]([a-z0-9.:-]{0,251}[a-z0-9])?$'),
  port             integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
  connection_limit integer NOT NULL CHECK (connection_limit BETWEEN 1 AND 100),
  created_at       timestamptz NOT NULL DEFAULT now(),
  rotated_at       timestamptz,
  PRIMARY KEY (org_id, environment_id),
  FOREIGN KEY (org_id, environment_id) REFERENCES ssc.environment (org_id, id)
);

CREATE TRIGGER app_database_refuse_truncate
  BEFORE TRUNCATE ON ssc.app_database
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.app_database ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.app_database FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.app_database USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.app_database TO ssc_app;
