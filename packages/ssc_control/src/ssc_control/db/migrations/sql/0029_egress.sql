-- SSC-053 · The cell egress proxy: the org's allowlist and each environment's proxy credentials.

-- The org's internet allowlist, which an org admin edits and the snapshot carries to the proxy.
-- A host name or a '*.' pattern standing for one label (ssc_contracts.egress checks it fully).
-- added_by_user_id is the admin who added it; approval_request_id the approved request that
-- added it, when an approval did. Removing a host deletes its row.
CREATE TABLE ssc.egress_host (
  org_id              text NOT NULL REFERENCES ssc.org (id),
  host                text NOT NULL
                        CHECK (length(host) <= 253
                               AND host ~ '^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'),
  added_by_user_id    text,
  approval_request_id text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, host),
  FOREIGN KEY (org_id, added_by_user_id) REFERENCES ssc.user_account (org_id, id),
  FOREIGN KEY (org_id, approval_request_id) REFERENCES ssc.approval_request (org_id, id)
);

-- An app environment's egress proxy credentials. The token is never here: the cell agent made
-- it and wrote the proxy URL into the environment's HTTPS_PROXY secret at secret_version; sha1
-- is base64 SHA-1 of the token, which the proxy checks it against. The snapshot carries the two
-- newest of each environment, so a new credential is valid before the old one goes; older ones
-- are deleted.
CREATE TABLE ssc.egress_credential (
  org_id          text NOT NULL,
  environment_id  text NOT NULL,
  credential_id   text NOT NULL CHECK (credential_id ~ '^[a-z0-9]{12}$'),
  sha1            text NOT NULL CHECK (sha1 ~ '^[A-Za-z0-9+/]{27}=$'),
  secret_version  text NOT NULL CHECK (secret_version ~ '^[1-9][0-9]{0,18}$'),
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, environment_id, credential_id),
  FOREIGN KEY (org_id, environment_id) REFERENCES ssc.environment (org_id, id)
);

CREATE TRIGGER egress_host_refuse_truncate
  BEFORE TRUNCATE ON ssc.egress_host
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();
CREATE TRIGGER egress_credential_refuse_truncate
  BEFORE TRUNCATE ON ssc.egress_credential
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.egress_host ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.egress_host FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.egress_host USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());
ALTER TABLE ssc.egress_credential ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.egress_credential FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.egress_credential USING (org_id = ssc.current_org())
  WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT, DELETE ON ssc.egress_host TO ssc_app;
GRANT SELECT, INSERT, DELETE ON ssc.egress_credential TO ssc_app;
