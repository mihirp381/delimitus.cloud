-- SSC-011 · Idempotency claims: the ledger that lets the API tell a retry from a second request.
--
-- Expand step. Mined from Delimitus `state/idempotency.ts` (claimed / settled / absent), with one
-- change that the control database makes possible: the claim lives in the SAME transaction as
-- the change it protects. A refused request rolls its claim back, so a refusal leaves no trace;
-- a request that dies mid-way rolls back too, so a row is only ever `claimed` while a request is
-- actually running. A second request with the same key blocks on the primary key until the
-- first commits and then reads the settled result.
--
-- The key is per credential: two credentials in one org may reuse a key without colliding.
-- Nothing here is customer content beyond the client's own key string; the response body is
-- what the API already returned to that credential.

CREATE TABLE ssc.idempotency_claim (
  org_id           text NOT NULL REFERENCES ssc.org (id),
  credential_id    text NOT NULL,
  key              text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
  request_hash     text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),  -- sha256(method, path, body)
  state            text NOT NULL DEFAULT 'claimed' CHECK (state IN ('claimed', 'settled')),
  status_code      integer CHECK (status_code BETWEEN 200 AND 299),  -- only successes settle
  response_body    jsonb,
  response_headers jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  settled_at       timestamptz,
  PRIMARY KEY (org_id, credential_id, key),
  CHECK ((state = 'settled') = (status_code IS NOT NULL)),
  CHECK ((state = 'settled') = (settled_at IS NOT NULL))
);
CREATE INDEX idempotency_claim_created_idx ON ssc.idempotency_claim (org_id, created_at);

ALTER TABLE ssc.idempotency_claim ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.idempotency_claim FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.idempotency_claim
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

-- No DELETE: a refused request rolls its claim back; reaping old claims is a migrator job
-- (retention period to be chosen with SSC-012), not something the app role does at 2am.
GRANT SELECT, INSERT, UPDATE ON ssc.idempotency_claim TO ssc_app;
