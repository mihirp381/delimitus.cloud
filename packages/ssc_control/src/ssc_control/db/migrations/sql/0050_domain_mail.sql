-- CD-6.3: the mail to an org's admins when its own address goes active or fails; no approval.
ALTER TABLE ssc.notification_outbox
  DROP CONSTRAINT notification_outbox_kind_check,
  ADD CONSTRAINT notification_outbox_kind_check CHECK (
    kind IN ('arrival', 'reminder', 'digest', 'decided', 'welcome', 'domain_active',
             'domain_failed')),
  DROP CONSTRAINT notification_outbox_check1,
  ADD CONSTRAINT notification_outbox_check1 CHECK (
    (kind IN ('digest', 'welcome', 'domain_active', 'domain_failed')) = (approval_id IS NULL));
