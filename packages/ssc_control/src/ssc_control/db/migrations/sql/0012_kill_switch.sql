-- SSC-025 (B5): ssc.kill_switch_run, one row per pull of an app's kill switch, with each step's
-- timings in `steps`. One run per app is running at a time. Plus the inventory's last-used index.

CREATE TABLE ssc.kill_switch_run (
  id                  text PRIMARY KEY CHECK (id ~ '^kil_[a-z0-9]{20}$'),
  org_id              text NOT NULL,
  app_id              text NOT NULL,
  mode                text NOT NULL CHECK (mode IN ('disable', 'quarantine')),
  state               text NOT NULL DEFAULT 'running'
                      CHECK (state IN ('running', 'completed', 'failed')),
  steps               jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(steps) = 'array'),
  paused_schedule_ids jsonb NOT NULL DEFAULT '[]'::jsonb
                      CHECK (jsonb_typeof(paused_schedule_ids) = 'array'),
  actor_kind          text NOT NULL CHECK (actor_kind IN ('user', 'workload', 'schedule', 'operator', 'integration')),
  actor_id            text NOT NULL,
  actor_via_agent     boolean NOT NULL DEFAULT false,
  actor_client_id     text,
  started_at          timestamptz NOT NULL DEFAULT now(),
  finished_at         timestamptz,
  resumed_at          timestamptz,
  CONSTRAINT kill_switch_run_finished_check CHECK ((state = 'running') = (finished_at IS NULL)),
  -- Enabling the app resumes the timers a finished run paused, once.
  CONSTRAINT kill_switch_run_resumed_check CHECK (resumed_at IS NULL OR state <> 'running'),
  UNIQUE (org_id, id),
  FOREIGN KEY (org_id, app_id) REFERENCES ssc.app (org_id, id)
);

CREATE UNIQUE INDEX kill_switch_one_running
  ON ssc.kill_switch_run (org_id, app_id)
  WHERE state = 'running';
CREATE INDEX kill_switch_run_app_idx ON ssc.kill_switch_run (org_id, app_id, started_at DESC);

ALTER TABLE ssc.kill_switch_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.kill_switch_run FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.kill_switch_run
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.kill_switch_run TO ssc_app;

CREATE INDEX metrics_event_last_used
  ON ssc.metrics_event (org_id, app_id, at DESC)
  WHERE kind = 'app_opened';
