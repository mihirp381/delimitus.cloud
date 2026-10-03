-- SSC-043 · Migration ledgers and recovery points.

-- The migrations the build found in the source, per ledger tool, in the order the tool applies
-- them: {"prisma": ["20261001120000_init", ...]}. Kept on the build while it runs and copied to
-- its release, where it is written once, at insert. NULL when the build did not read the source.
ALTER TABLE ssc.build
  ADD COLUMN migrations jsonb CHECK (migrations IS NULL OR jsonb_typeof(migrations) = 'object');

ALTER TABLE ssc.release
  ADD COLUMN migrations jsonb CHECK (migrations IS NULL OR jsonb_typeof(migrations) = 'object');

-- Every migration of every release the deploy job has handed to the runtime for this
-- environment, in the same shape: what the database may have run. A rollback to a release that
-- lacks one of them is refused unless it is confirmed.
ALTER TABLE ssc.app_database
  ADD COLUMN migrations jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(migrations) = 'object');

-- Where the instance was before a production deployment of an environment with a database
-- started: the database server's time and write-ahead log position, or the control plane's time
-- alone when the cell could not say. A restore to it follows the runbook.
ALTER TABLE ssc.deployment
  ADD COLUMN recovery_at timestamptz,
  ADD COLUMN recovery_lsn text CHECK (recovery_lsn ~ '^[0-9A-F]{1,8}/[0-9A-F]{1,8}$'),
  ADD CONSTRAINT deployment_recovery_check CHECK (recovery_lsn IS NULL OR recovery_at IS NOT NULL);
