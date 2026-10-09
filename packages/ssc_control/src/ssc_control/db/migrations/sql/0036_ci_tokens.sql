-- GA-7.7 · CI tokens.

-- A repository secret cannot hold anything a person has: an access token lasts five minutes, a
-- session twelve hours, and refresh tokens rotate. A CI token is an access token whose sid names
-- a session of kind 'ci': scope 'preview' (the API never lets it touch production), a label the
-- person chose, up to 90 days from creation, never a refresh token (code: only command-line and
-- console sessions are refreshed) and never an agent (auth_session_agent_check). The API checks
-- its session on every call, so revoking it, its expiry and deactivating its person end it.
-- 'revoked': a person or an org admin revoked a CI token.
ALTER TABLE ssc.auth_session
  ADD COLUMN scope text CHECK (scope = 'preview'),
  ADD COLUMN label text CHECK (length(label) BETWEEN 1 AND 100 AND label !~ '[[:cntrl:]]');

-- auth_session_check is the twelve-hour lifetime of 0014, named by Postgres.
ALTER TABLE ssc.auth_session
  DROP CONSTRAINT auth_session_kind_check,
  ADD CONSTRAINT auth_session_kind_check CHECK (kind IN ('browser', 'cli', 'console', 'ci')),
  DROP CONSTRAINT auth_session_revoke_reason_check,
  ADD CONSTRAINT auth_session_revoke_reason_check CHECK (revoke_reason IN (
    'logout', 'user_deactivated', 'refresh_reuse', 'operator', 'code_reuse', 'revoked')),
  DROP CONSTRAINT auth_session_check,
  ADD CONSTRAINT auth_session_lifetime_check CHECK (expires_at > created_at AND expires_at <=
    created_at + CASE WHEN kind = 'ci' THEN interval '90 days' ELSE interval '12 hours' END),
  ADD CONSTRAINT auth_session_ci_check CHECK (
    (kind = 'ci') = (scope IS NOT NULL) AND (kind = 'ci') = (label IS NOT NULL)
    AND (kind <> 'ci' OR token_audience IS NULL));
