-- Pilot social sign-in (decision 002 amendment 2026-10-27): an operator puts a pilot org in
-- social mode; its admins invite people by email, and a first sign-in links the provider id.
ALTER TABLE ssc.org
  ADD COLUMN sign_in_mode text NOT NULL DEFAULT 'directory'
    CHECK (sign_in_mode IN ('directory', 'social')),
  ADD COLUMN social_providers text[] NOT NULL DEFAULT '{}'
    CHECK (social_providers <@ ARRAY['google', 'microsoft', 'github']::text[]),
  ADD CONSTRAINT org_social_needs_provider
    CHECK (sign_in_mode = 'directory' OR cardinality(social_providers) > 0);

ALTER TABLE ssc.user_account
  DROP CONSTRAINT user_account_status_check,
  ADD CONSTRAINT user_account_status_check
    CHECK (status IN ('active', 'invited', 'deactivated'));

ALTER TABLE ssc.identity_link
  DROP CONSTRAINT identity_link_source_check,
  ADD CONSTRAINT identity_link_source_check CHECK (source IN ('directory', 'admin', 'social'));

ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'org.updated',
    'user.created', 'user.updated', 'user.deactivated', 'user.reactivated',
    'user.invited',
    'group.synced',
    'app.created', 'app.owner_transferred', 'app.disabled', 'app.quarantined',
    'app.enabled', 'app.deleted',
    'login.succeeded', 'login.failed', 'token.issued', 'token.revoked',
    'auth.authorize_approved', 'auth.authorize_denied', 'auth.code_reused',
    'secret.bound', 'secret.rotated', 'secret.removed',
    'grant.added', 'grant.removed',
    'bundle.stored', 'build.started', 'build.failed', 'release.created',
    'deploy.started', 'deploy.finished', 'deploy.failed',
    'rollback.started', 'rollback.finished', 'rollback.failed',
    'kill_switch.step',
    'approval.requested', 'approval.decided', 'approval.cancelled',
    'schedule.created', 'schedule.updated', 'schedule.paused', 'schedule.resumed',
    'schedule.deleted', 'schedule.run_requested',
    'connection.created', 'connection.removed',
    'connection.updated', 'connection.granted', 'connection.revoked',
    'connection.ceiling_lowered', 'connection.flagged',
    'operator.access',
    'audit.exported', 'audit.reanchored',
    'directory.connected', 'directory.frozen', 'identity.linked',
    'cell.resource_requested', 'cell.resource_ready', 'cell.resource_failed',
    'github.installation_bound', 'repo.connected', 'repo.disconnected',
    'cell.requested', 'cell.ready', 'cell.failed', 'cell.retired',
    'org.onboarded',
    'invite.created', 'invite.revoked', 'invite.used'));
