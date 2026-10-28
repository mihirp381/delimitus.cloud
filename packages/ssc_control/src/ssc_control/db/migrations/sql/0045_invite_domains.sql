-- Decision 002 amendment 2026-10-28: a social org invites only addresses at its own domains.
ALTER TABLE ssc.org NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.user_account NO FORCE ROW LEVEL SECURITY;

ALTER TABLE ssc.org ADD COLUMN invite_domains text[] NOT NULL DEFAULT '{}';

UPDATE ssc.org o
  SET invite_domains = coalesce(
    (SELECT array_agg(DISTINCT lower(regexp_replace(u.email, '^.*@', ''))
            ORDER BY lower(regexp_replace(u.email, '^.*@', '')))
       FROM ssc.user_account u
      WHERE u.org_id = o.id AND u.status IN ('invited', 'active')),
    '{}')
  WHERE o.sign_in_mode = 'social';

ALTER TABLE ssc.org
  ADD CONSTRAINT org_invite_domains_by_mode CHECK (
    CASE WHEN sign_in_mode = 'social' THEN cardinality(invite_domains) BETWEEN 1 AND 10
         ELSE cardinality(invite_domains) = 0 END),
  ADD CONSTRAINT org_invite_domains_shape CHECK (
    array_position(invite_domains, NULL) IS NULL
    AND array_position(invite_domains, '') IS NULL
    AND array_to_string(invite_domains, ',') = lower(array_to_string(invite_domains, ','))
    AND strpos(array_to_string(invite_domains, ','), '@') = 0);

ALTER TABLE ssc.user_account FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.org FORCE ROW LEVEL SECURITY;
