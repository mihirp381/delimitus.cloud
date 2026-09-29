-- SSC-041 (A5) · Timers: what a schedule calls, when it runs next, and a record of every run.
-- Decision 020. Nothing wrote ssc.schedule before this revision, so the NOT NULL columns are safe.

-- The foreign key's check of existing rows is filtered by RLS, which raises with no org bound;
-- FORCE is lifted for this transaction only, as in 0005, and restored below.
ALTER TABLE ssc.schedule NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.user_account NO FORCE ROW LEVEL SECURITY;

ALTER TABLE ssc.schedule
  ADD COLUMN path                text NOT NULL
                                   CHECK (length(path) <= 512 AND path ~ '^/[!-~]*$'),
  ADD COLUMN method              text NOT NULL DEFAULT 'POST' CHECK (method IN ('GET', 'POST')),
  ADD COLUMN timeout_seconds     integer NOT NULL DEFAULT 60
                                   CHECK (timeout_seconds BETWEEN 1 AND 900),
  ADD COLUMN pause_reason        text CHECK (pause_reason IN (
                                   'manual', 'preview', 'app_disabled', 'app_quarantined',
                                   'owner_deactivated', 'builder_access_revoked')),
  ADD COLUMN next_run_at         timestamptz,
  ADD COLUMN last_scheduled_for  timestamptz,
  ADD COLUMN declared_by_user_id text NOT NULL,
  -- A pause always says why; an active schedule is always armed for its next instant.
  ADD CONSTRAINT schedule_paused_has_reason CHECK ((state = 'paused') = (pause_reason IS NOT NULL)),
  ADD CONSTRAINT schedule_active_is_armed CHECK ((state = 'active') = (next_run_at IS NOT NULL)),
  ADD CONSTRAINT schedule_declared_by_fk
    FOREIGN KEY (org_id, declared_by_user_id) REFERENCES ssc.user_account (org_id, id);

ALTER TABLE ssc.schedule FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.user_account FORCE ROW LEVEL SECURITY;

-- A deleted schedule keeps its name and its runs; the name may be declared again as a new one.
ALTER TABLE ssc.schedule DROP CONSTRAINT schedule_org_id_environment_id_name_key;
CREATE UNIQUE INDEX schedule_live_name ON ssc.schedule (org_id, environment_id, name)
  WHERE state <> 'deleted';
-- The re-arm sweep's scan: active schedules by instant.
CREATE INDEX schedule_armed ON ssc.schedule (org_id, next_run_at) WHERE state = 'active';

-- One row per run: a scheduled instant the worker claimed, or a manual run someone asked for.
-- No response bodies and no personal data. Never deleted by the application.
CREATE TABLE ssc.timer_run (
  id                   text PRIMARY KEY CHECK (id ~ '^tmr_[a-z0-9]{20}$'),
  org_id               text NOT NULL,
  schedule_id          text NOT NULL,
  trigger              text NOT NULL CHECK (trigger IN ('schedule', 'manual')),
  scheduled_for        timestamptz NOT NULL,
  requested_by_user_id text,
  state                text NOT NULL CHECK (state IN (
                         'queued', 'running', 'succeeded', 'failed', 'timed_out', 'skipped')),
  error                text CHECK (error IN (
                         'overlap', 'app_inactive', 'owner_inactive', 'builder_access_revoked',
                         'deleted', 'dispatch_unavailable', 'dispatch_error', 'http_error',
                         'timeout', 'abandoned')),
  http_status          integer CHECK (http_status BETWEEN 100 AND 599),
  duration_ms          integer CHECK (duration_ms >= 0),
  created_at           timestamptz NOT NULL DEFAULT now(),
  started_at           timestamptz,
  finished_at          timestamptz,
  UNIQUE (org_id, id),
  CHECK ((trigger = 'manual') = (requested_by_user_id IS NOT NULL)),
  CHECK (state <> 'queued' OR trigger = 'manual'),
  CHECK ((state IN ('queued', 'skipped')) = (started_at IS NULL)),
  CHECK ((state IN ('queued', 'running')) = (finished_at IS NULL)),
  CHECK ((state IN ('queued', 'running', 'succeeded')) = (error IS NULL)),
  CHECK (state <> 'succeeded' OR http_status BETWEEN 200 AND 299),
  FOREIGN KEY (org_id, schedule_id) REFERENCES ssc.schedule (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, requested_by_user_id) REFERENCES ssc.user_account (org_id, id)
);

-- No overlap: at most one run of a schedule is running, and at most one manual run waits.
CREATE UNIQUE INDEX timer_run_one_running ON ssc.timer_run (org_id, schedule_id)
  WHERE state = 'running';
CREATE UNIQUE INDEX timer_run_one_queued ON ssc.timer_run (org_id, schedule_id)
  WHERE state = 'queued';
-- The backstop against firing one scheduled instant twice.
CREATE UNIQUE INDEX timer_run_once_per_instant ON ssc.timer_run (org_id, schedule_id, scheduled_for)
  WHERE trigger = 'schedule';
-- History, newest first, and the sweep's scan of unfinished runs.
CREATE INDEX timer_run_history ON ssc.timer_run (org_id, schedule_id, scheduled_for DESC, id DESC);
CREATE INDEX timer_run_unfinished ON ssc.timer_run (org_id, state)
  WHERE state IN ('queued', 'running');

ALTER TABLE ssc.timer_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.timer_run FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.timer_run
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, UPDATE ON ssc.timer_run TO ssc_app;
