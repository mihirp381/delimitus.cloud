-- GA-12 step 0.5 · removing an app.

-- An app an org admin removes is 'deleting' from the request on (denied everywhere, its removal
-- run under way or stopped at a failed step, which running it again finishes) and 'deleted' once
-- the run is through. The row stays as a tombstone: releases are immutable, and deployments,
-- builds, bundles, runs, metrics and the audit log all name the app, so nothing is deleted from
-- them and every foreign key holds.
ALTER TABLE ssc.app
  DROP CONSTRAINT app_status_check,
  ADD CONSTRAINT app_status_check
    CHECK (status IN ('active', 'disabled', 'quarantined', 'deleting', 'deleted'));

-- A deleted app gives its name back. Nothing in a cell is named by the slug but the address,
-- which the access snapshot stops listing for an app being deleted; everything else is named by
-- the environment's id, which is never reused.
ALTER TABLE ssc.app DROP CONSTRAINT app_org_id_slug_key;
CREATE UNIQUE INDEX app_slug_live ON ssc.app (org_id, slug) WHERE status <> 'deleted';

-- The removal is a run of the kill switch's saga with its own steps.
ALTER TABLE ssc.kill_switch_run
  DROP CONSTRAINT kill_switch_run_mode_check,
  ADD CONSTRAINT kill_switch_run_mode_check CHECK (mode IN ('disable', 'quarantine', 'delete'));

-- The run drops an environment's database in its cell and then forgets where it was, so the
-- org's count of places taken follows.
GRANT DELETE ON ssc.app_database TO ssc_app;
