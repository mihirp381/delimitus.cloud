-- Ticket 19 · The cells this control plane serves (decision 030 amendment), one row per cell,
-- written by the operator (python -m ssc_control.cells) and read live by the router. Platform
-- data, not an org's: no RLS (db/README.md rule 18). org_id is the org the cell was built for,
-- with no foreign key, since a cell may be built before its org exists. A ready cell needs its
-- public JWKS only: cells imported from SSC_CELLS have no project number.
CREATE TABLE ssc.cell (
  label          text PRIMARY KEY CHECK (label ~ '^[a-z][a-z0-9]{7,15}$'),
  stage          text NOT NULL CHECK (stage IN ('staging', 'prod')),
  status         text NOT NULL CHECK (status IN ('building', 'ready', 'failed', 'retired')),
  identity_jwks  jsonb CHECK (jsonb_typeof(identity_jwks) = 'object'),
  project_number bigint CHECK (project_number > 0),
  org_id         text UNIQUE CHECK (org_id ~ '^org_[a-z0-9]{20}$'),
  last_error     text CHECK (length(last_error) BETWEEN 1 AND 500),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT cell_ready_check CHECK (status <> 'ready' OR identity_jwks IS NOT NULL)
);

CREATE TRIGGER cell_refuse_truncate
  BEFORE TRUNCATE ON ssc.cell
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

GRANT SELECT, INSERT, UPDATE ON ssc.cell TO ssc_app;
