-- SSC-041: a timer run first asks the app's health_path through its public host, so a gateway
-- and an app at zero start before the run's own clock does. start_ms is how long that took; a
-- run whose start got no answer below 500 fails with start_failed.
ALTER TABLE ssc.timer_run
  ADD COLUMN start_ms integer,
  ADD CONSTRAINT timer_run_start_ms_check CHECK (start_ms >= 0),
  DROP CONSTRAINT timer_run_error_check,
  ADD CONSTRAINT timer_run_error_check CHECK (error IN (
    'overlap', 'app_inactive', 'owner_inactive', 'builder_access_revoked',
    'deleted', 'dispatch_unavailable', 'dispatch_error', 'http_error',
    'timeout', 'abandoned', 'start_failed'));
