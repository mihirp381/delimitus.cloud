-- Founder decision 2026-10-26 (decision 016 amendment): an org admin may decide their own
-- request. check_decider still refuses a non-admin connection owner deciding their own.
ALTER TABLE ssc.approval_request DROP CONSTRAINT approval_request_not_self;
