-- SSC-026 · Secrets.

-- ssc.secret_ref (revision 0001) holds a reference and a version, never a value. The version is
-- the Secret Manager version number the secret intake returned; updated_at is when it was set.
ALTER TABLE ssc.secret_ref
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
  ADD CONSTRAINT secret_ref_version_check CHECK (secret_version ~ '^[1-9][0-9]{0,18}$');

-- The secret versions a deployment runs, {"NAME": "<version>"}, copied from ssc.secret_ref when
-- the deploy job first claims it and never changed after, so a failed rotation leaves the live
-- deployment's versions in place and the reconciler applies exactly what went live. NULL until
-- that first claim.
ALTER TABLE ssc.deployment
  ADD COLUMN secret_refs jsonb CHECK (secret_refs IS NULL OR jsonb_typeof(secret_refs) = 'object');
