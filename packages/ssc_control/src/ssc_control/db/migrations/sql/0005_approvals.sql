-- SSC-045 · Approvals: the environment profile, and the approval_request shape the approvals
-- service needs (decision 016).
--
-- Expand step. approval_request has had no writer since 0001, so it is empty everywhere and the
-- new NOT NULL columns need no default; on a table that somehow holds rows this revision fails
-- instead of guessing an environment. environment.profile has a default, so the previous release
-- keeps inserting environments unchanged.

ALTER TABLE ssc.environment
  ADD COLUMN profile text NOT NULL DEFAULT 'internal'
    CONSTRAINT environment_profile_check CHECK (profile IN ('internal'));

-- Adding a foreign key checks the existing rows with a query that row-level security filters
-- through ssc.current_org(), which raises when no org is bound. The owner is exempt from RLS
-- unless it is forced, so FORCE is lifted for this transaction only and restored below. ssc_app
-- owns nothing, so it stays under RLS throughout.
ALTER TABLE ssc.approval_request NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.environment NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.policy_decision NO FORCE ROW LEVEL SECURITY;

ALTER TABLE ssc.approval_request
  ADD COLUMN environment_id       text NOT NULL,
  ADD COLUMN subject_key          text NOT NULL
    CONSTRAINT approval_request_subject_key_check CHECK (length(subject_key) BETWEEN 1 AND 300),
  ADD COLUMN decision_reason      text
    CONSTRAINT approval_request_decision_reason_check CHECK (length(decision_reason) BETWEEN 1 AND 500),
  ADD COLUMN decision_channel     text
    CONSTRAINT approval_request_decision_channel_check CHECK (decision_channel IN ('email', 'chat', 'console')),
  ADD COLUMN recorded_by_operator text
    CONSTRAINT approval_request_recorded_by_operator_check CHECK (length(recorded_by_operator) BETWEEN 1 AND 200),
  ADD COLUMN policy_decision_id   text,
  ADD CONSTRAINT approval_request_kind_check CHECK (
    kind IN ('widen_audience', 'connect_data_source', 'enable_internet_hosts', 'agent_share')),
  -- A decision always says why; a pending request carries no decision fields at all.
  ADD CONSTRAINT approval_request_reason_check CHECK ((state = 'pending') = (decision_reason IS NULL)),
  ADD CONSTRAINT approval_request_pending_undecided_check CHECK (
    state <> 'pending'
    OR (decision_channel IS NULL AND recorded_by_operator IS NULL AND policy_decision_id IS NULL)),
  ADD CONSTRAINT approval_request_environment_fkey FOREIGN KEY (org_id, environment_id)
    REFERENCES ssc.environment (org_id, id) ON DELETE CASCADE,
  ADD CONSTRAINT approval_request_policy_decision_fkey FOREIGN KEY (org_id, policy_decision_id)
    REFERENCES ssc.policy_decision (org_id, id);

ALTER TABLE ssc.approval_request FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.environment FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.policy_decision FORCE ROW LEVEL SECURITY;

-- 0001 left the self-approval CHECK unnamed. Find it by its definition, not by a guessed
-- generated name, and replace it with a named one that also covers denials: nobody decides
-- their own request either way. The requester may still cancel (withdraw) it.
DO $$
DECLARE
  c name;
BEGIN
  SELECT conname INTO STRICT c
    FROM pg_constraint
   WHERE conrelid = 'ssc.approval_request'::regclass AND contype = 'c'
     AND pg_get_constraintdef(oid) LIKE '%decided_by_user_id IS DISTINCT FROM requested_by_user_id%';
  EXECUTE format('ALTER TABLE ssc.approval_request DROP CONSTRAINT %I', c);
END;
$$;

ALTER TABLE ssc.approval_request
  ADD CONSTRAINT approval_request_not_self CHECK (
    state NOT IN ('approved', 'denied') OR decided_by_user_id IS DISTINCT FROM requested_by_user_id);

-- One open request per question; the service returns it instead of opening a second.
CREATE UNIQUE INDEX approval_request_one_pending
  ON ssc.approval_request (org_id, environment_id, kind, subject_key)
  WHERE state = 'pending';
-- The newest request per question (gate and sharing checks), and newest-first listing.
CREATE INDEX approval_request_subject_idx
  ON ssc.approval_request (org_id, environment_id, kind, subject_key, created_at DESC);
CREATE INDEX approval_request_created_idx
  ON ssc.approval_request (org_id, created_at DESC, id DESC);
