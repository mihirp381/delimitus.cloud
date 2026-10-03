-- SSC-090: how long Cloud Run lets one request to the environment run, as the gateway may tell
-- the app (X-SSC-Request-Deadline). A deployment lowers it before a revision with a shorter
-- timeout can get traffic and raises it only after a revision with a longer one has it, so it is
-- never longer than the timeout of the revision serving. NULL means the request-billed figure.
ALTER TABLE ssc.environment
  ADD COLUMN request_timeout_seconds integer,
  ADD CONSTRAINT environment_request_timeout_seconds_check CHECK (request_timeout_seconds > 0);
