-- W0 · Lane vocabulary: every audit action the three lanes are known to emit, added once before
-- they branch so no two lanes rewrite this CHECK.
--
-- Expand step: the list only grows, so the previous release (which writes a subset) keeps
-- working. New: audit.exported, audit.reanchored, user.updated, schedule.updated,
-- schedule.run_requested (lane A); bundle.stored, build.started, build.failed, release.created
-- (lane B). The constraint keeps its name so the next revision can find it.

ALTER TABLE ssc.audit_event
  DROP CONSTRAINT audit_event_action_check,
  ADD CONSTRAINT audit_event_action_check CHECK (action IN (
    'org.created', 'user.created', 'user.updated', 'user.deactivated', 'user.reactivated',
    'group.synced',
    'app.created', 'app.owner_transferred', 'app.disabled', 'app.quarantined',
    'app.enabled', 'app.deleted',
    'login.succeeded', 'login.failed', 'token.issued', 'token.revoked',
    'secret.bound', 'secret.rotated', 'secret.removed',
    'grant.added', 'grant.removed',
    'bundle.stored', 'build.started', 'build.failed', 'release.created',
    'deploy.started', 'deploy.finished', 'deploy.failed',
    'rollback.started', 'rollback.finished', 'rollback.failed',
    'kill_switch.step',
    'approval.requested', 'approval.decided',
    'schedule.created', 'schedule.updated', 'schedule.paused', 'schedule.resumed',
    'schedule.deleted', 'schedule.run_requested',
    'connection.created', 'connection.removed',
    'operator.access',
    'audit.exported', 'audit.reanchored'));
