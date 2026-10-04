-- SSC-092 · The warm option: production environments and the cell's gateway kept at one instance.

-- An org admin names a production environment to keep at minimum 1; the runtime driver reads it
-- (desired_for). Preview environments are never warm.
ALTER TABLE ssc.environment
  ADD COLUMN warm boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT environment_warm_prod_check CHECK (NOT warm OR name = 'prod');

-- The gateway's part, carried by the cell stack's warm flag, which the cell deployer sets.
-- wanted is what the admin chose; applied is what the last successful run set (a cell starts
-- with the flag off); execution is the run in flight, setting execution_wants. attempts counts
-- the runs since wanted last changed; failure_code is set when they ran out.
CREATE TABLE ssc.warm_gateway (
  org_id          text PRIMARY KEY REFERENCES ssc.org (id),
  wanted          boolean NOT NULL,
  applied         boolean NOT NULL DEFAULT false,
  execution       text CHECK (length(execution) <= 300),
  execution_wants boolean,
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  failure_code    text CHECK (failure_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  last_error      text CHECK (length(last_error) <= 200),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  applied_at      timestamptz,
  CHECK ((execution IS NULL) = (execution_wants IS NULL))
);

CREATE TRIGGER warm_gateway_refuse_truncate
  BEFORE TRUNCATE ON ssc.warm_gateway
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.warm_gateway ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.warm_gateway FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.warm_gateway USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.warm_gateway TO ssc_app;
