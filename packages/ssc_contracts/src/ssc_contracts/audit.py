"""The closed list of audit actions and actor kinds. Mirrored by CHECK constraints in the control
database; a catalog test fails when the two drift. Adding an action is an expand migration."""

from enum import StrEnum


class ActorKind(StrEnum):
    USER = "user"
    WORKLOAD = "workload"  # an app environment acting for itself
    SCHEDULE = "schedule"  # a timer run; never delegated from a user
    OPERATOR = "operator"  # SSC staff, always through a recorded entitlement
    INTEGRATION = "integration"  # a connection or a GitHub App installation


class AuditAction(StrEnum):
    ORG_CREATED = "org.created"
    ORG_UPDATED = "org.updated"
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DEACTIVATED = "user.deactivated"
    USER_REACTIVATED = "user.reactivated"
    GROUP_SYNCED = "group.synced"
    APP_CREATED = "app.created"
    APP_OWNER_TRANSFERRED = "app.owner_transferred"
    APP_DISABLED = "app.disabled"
    APP_QUARANTINED = "app.quarantined"
    APP_ENABLED = "app.enabled"
    APP_DELETED = "app.deleted"
    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    TOKEN_ISSUED = "token.issued"  # noqa: S105  (an action name, not a secret)
    TOKEN_REVOKED = "token.revoked"  # noqa: S105  (an action name, not a secret)
    AUTHORIZE_APPROVED = "auth.authorize_approved"
    AUTHORIZE_DENIED = "auth.authorize_denied"
    CODE_REUSED = "auth.code_reused"
    SECRET_BOUND = "secret.bound"  # noqa: S105  (an action name, not a secret)
    SECRET_ROTATED = "secret.rotated"  # noqa: S105  (an action name, not a secret)
    SECRET_REMOVED = "secret.removed"  # noqa: S105  (an action name, not a secret)
    GRANT_ADDED = "grant.added"
    GRANT_REMOVED = "grant.removed"
    BUNDLE_STORED = "bundle.stored"
    BUILD_STARTED = "build.started"
    BUILD_FAILED = "build.failed"
    RELEASE_CREATED = "release.created"
    DEPLOY_STARTED = "deploy.started"
    DEPLOY_FINISHED = "deploy.finished"
    DEPLOY_FAILED = "deploy.failed"
    ROLLBACK_STARTED = "rollback.started"
    ROLLBACK_FINISHED = "rollback.finished"
    ROLLBACK_FAILED = "rollback.failed"
    KILL_SWITCH_STEP = "kill_switch.step"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_CANCELLED = "approval.cancelled"
    SCHEDULE_CREATED = "schedule.created"
    SCHEDULE_UPDATED = "schedule.updated"
    SCHEDULE_PAUSED = "schedule.paused"
    SCHEDULE_RESUMED = "schedule.resumed"
    SCHEDULE_DELETED = "schedule.deleted"
    SCHEDULE_RUN_REQUESTED = "schedule.run_requested"
    CONNECTION_CREATED = "connection.created"
    CONNECTION_REMOVED = "connection.removed"
    CONNECTION_UPDATED = "connection.updated"
    CONNECTION_GRANTED = "connection.granted"
    CONNECTION_REVOKED = "connection.revoked"
    CONNECTION_CEILING_LOWERED = "connection.ceiling_lowered"
    CONNECTION_FLAGGED = "connection.flagged"
    OPERATOR_ACCESS = "operator.access"
    AUDIT_EXPORTED = "audit.exported"
    AUDIT_REANCHORED = "audit.reanchored"
    DIRECTORY_CONNECTED = "directory.connected"
    DIRECTORY_FROZEN = "directory.frozen"
    IDENTITY_LINKED = "identity.linked"
    CELL_RESOURCE_REQUESTED = "cell.resource_requested"
    CELL_RESOURCE_READY = "cell.resource_ready"
    CELL_RESOURCE_FAILED = "cell.resource_failed"
    GITHUB_INSTALLATION_BOUND = "github.installation_bound"
    REPO_CONNECTED = "repo.connected"
    REPO_DISCONNECTED = "repo.disconnected"
