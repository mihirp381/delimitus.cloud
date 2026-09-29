-- SSC-012 (A1b) · Audit anchors, a cell label for every org, and snapshot content digests.

-- Each anchor of an org's audit head written to the blob store (decision 012). Append-only.
CREATE TABLE ssc.audit_anchor (
  org_id      text NOT NULL REFERENCES ssc.org (id),
  anchored_at timestamptz NOT NULL,
  seq         bigint NOT NULL CHECK (seq >= 0),
  hash        bytea NOT NULL CHECK (octet_length(hash) = 32),
  object_key  text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 512),
  reason      text NOT NULL CHECK (reason IN ('daily', 'restore')),
  restored_to timestamptz,
  CHECK ((reason = 'restore') = (restored_to IS NOT NULL)),
  PRIMARY KEY (org_id, anchored_at),
  UNIQUE (org_id, object_key)
);

CREATE TRIGGER audit_anchor_append_only
  BEFORE UPDATE OR DELETE ON ssc.audit_anchor
  FOR EACH ROW EXECUTE FUNCTION ssc.refuse_row_change('SC005');
CREATE TRIGGER audit_anchor_refuse_truncate
  BEFORE TRUNCATE ON ssc.audit_anchor
  FOR EACH STATEMENT EXECUTE FUNCTION ssc.refuse_truncate();

ALTER TABLE ssc.audit_anchor ENABLE ROW LEVEL SECURITY;
ALTER TABLE ssc.audit_anchor FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON ssc.audit_anchor
  USING (org_id = ssc.current_org()) WITH CHECK (org_id = ssc.current_org());

GRANT SELECT, INSERT ON ssc.audit_anchor TO ssc_app;

-- Every org gets its opaque cell label when it is created (founder default D1: app hosts are
-- <slug>[--preview].<cell_label>.<apps domain>). Twelve consonants from 48 random bits, so a
-- label never spells a word. The default serves every writer, the previous release included,
-- which is what makes NOT NULL safe here. Back-fill: owner lifts FORCE on ssc.org.
ALTER TABLE ssc.org ALTER COLUMN cell_label SET DEFAULT translate(
  substr(replace(gen_random_uuid()::text, '-', ''), 1, 12), '0123456789abcdef', 'bcdfghjkmnpqrstv'
);
ALTER TABLE ssc.org NO FORCE ROW LEVEL SECURITY;
UPDATE ssc.org SET cell_label = DEFAULT WHERE cell_label IS NULL;
ALTER TABLE ssc.org ALTER COLUMN cell_label SET NOT NULL;
ALTER TABLE ssc.org FORCE ROW LEVEL SECURITY;

-- The digest of a snapshot's content without its version and time, so a sweep can tell a
-- stale snapshot from a current one. NULL for rows published before this revision.
ALTER TABLE ssc.access_snapshot
  ADD COLUMN content_digest text CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$');
