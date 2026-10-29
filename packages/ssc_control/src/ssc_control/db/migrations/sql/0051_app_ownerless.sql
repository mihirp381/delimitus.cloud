-- Decision 019 amendment 2026-10-29: the mail to an org's admins when an app's owner leaves.
-- FORCE is lifted for the foreign key's validation query and restored, as 0031 does.
ALTER TABLE ssc.notification_outbox NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.app NO FORCE ROW LEVEL SECURITY;

ALTER TABLE ssc.notification_outbox
  ADD COLUMN app_id text,
  ADD CONSTRAINT notification_outbox_app_fkey
    FOREIGN KEY (org_id, app_id) REFERENCES ssc.app (org_id, id),
  ADD CONSTRAINT notification_outbox_app_check CHECK ((kind = 'app_ownerless') = (app_id IS NOT NULL)),
  DROP CONSTRAINT notification_outbox_kind_check,
  ADD CONSTRAINT notification_outbox_kind_check CHECK (
    kind IN ('arrival', 'reminder', 'digest', 'decided', 'welcome', 'domain_active',
             'domain_failed', 'app_ownerless')),
  DROP CONSTRAINT notification_outbox_check1,
  ADD CONSTRAINT notification_outbox_check1 CHECK (
    (kind IN ('digest', 'welcome', 'domain_active', 'domain_failed', 'app_ownerless'))
      = (approval_id IS NULL));

ALTER TABLE ssc.notification_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.app FORCE ROW LEVEL SECURITY;
