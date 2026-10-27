-- Console People roster (decision 019 amendment 2026-10-26): each person's latest sign-in.
CREATE INDEX auth_session_last_sign_in ON ssc.auth_session (org_id, user_id, created_at DESC);
