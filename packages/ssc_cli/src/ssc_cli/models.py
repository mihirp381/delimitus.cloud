"""What the CLI reads from and sends to ``/v1``.

Response models ignore fields they do not know, so an additive API change never breaks an older
CLI. Enumerations are plain strings for the same reason. A test checks every field here against
``docs/api/openapi.json``.
"""

from pydantic import BaseModel, ConfigDict, Field


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Whoami(Wire):
    org_id: str
    subject: str
    kind: str
    credential_id: str
    is_agent: bool
    client_id: str | None = None
    role: str | None = None


class AppSummary(Wire):
    id: str
    slug: str
    owner_user_id: str
    status: str


class AppList(Wire):
    apps: list[AppSummary]


class EnvironmentOut(Wire):
    id: str
    name: str
    config_version: int
    grants_version: int
    current_deployment_id: str | None = None
    url: str | None = None


class AppOut(Wire):
    id: str
    slug: str
    owner_user_id: str
    status: str
    created_at: str
    environments: list[EnvironmentOut]


class GrantOut(Wire):
    id: str
    role: str
    subject_kind: str
    subject_id: str | None = None


class GrantsOut(Wire):
    environment_id: str
    grants_version: int
    grants: list[GrantOut]


class GrantsPending(Wire):
    """``202`` from ``PUT .../grants``: the change waits for approval and nothing was applied."""

    environment_id: str
    grants_version: int
    approval_ids: list[str]


class OperationOut(Wire):
    operation_id: str
    kind: str
    state: str
    app_id: str
    environment_id: str
    release_id: str | None = None
    started_at: str
    finished_at: str | None = None
    failure_code: str | None = None
    notice: str | None = None
    billing: str | None = None


class OperationAccepted(Wire):
    operation_id: str
    state: str
    notice: str | None = None


class UploadTarget(Wire):
    """Where to PUT a bundle or a secret's value. ``url`` is a credential: never print or log it."""

    method: str
    url: str
    headers: dict[str, str]
    expires_at: str


class BundleOut(Wire):
    bundle_id: str
    app_id: str
    digest: str
    size_bytes: int
    state: str
    manifest_digest: str | None = None
    upload: UploadTarget | None = None


class CapabilityChange(Wire):
    severity: str
    kind: str
    subject: str
    consequence: str
    approver: str | None = None


class CapabilityDiff(Wire):
    changes: list[CapabilityChange]
    total: int


class BuildAccepted(Wire):
    build_id: str
    state: str
    capability_diff: CapabilityDiff


class BuildOut(Wire):
    build_id: str
    app_id: str
    environment_id: str
    bundle_id: str
    state: str
    release_id: str | None = None
    release_number: int | None = None
    failure_code: str | None = None


class ActorOut(Wire):
    kind: str
    id: str
    via_agent: bool


class ReleaseOut(Wire):
    release_id: str
    number: int
    label: str
    image_digest: str
    source_digest: str
    source_commit: str | None = None
    built_for_environment_id: str | None = None
    latest_migrations: dict[str, str] | None = None
    created_at: str
    actor: ActorOut


class ReleaseList(Wire):
    items: list[ReleaseOut]
    next_before: int | None = None


class LedgerAhead(Wire):
    ledger: str
    names: list[str]


class MigrationsAhead(Wire):
    environment_id: str
    release_id: str
    ledgers: list[LedgerAhead]


class UserMatch(Wire):
    id: str
    display_name: str
    email: str
    role: str
    status: str


class UserMatches(Wire):
    users: list[UserMatch]


class GroupMatch(Wire):
    id: str
    name: str
    member_count: int


class GroupMatches(Wire):
    groups: list[GroupMatch]


class KillSwitchAccepted(Wire):
    run_id: str
    state: str


class KillSwitchStep(Wire):
    name: str
    state: str
    snapshot_version: int | None = None
    started_at: str
    finished_at: str | None = None
    elapsed_ms: int | None = None
    attempts: int
    error: str | None = None


class KillSwitchRun(Wire):
    run_id: str
    app_id: str
    mode: str
    state: str
    steps: list[KillSwitchStep]
    started_at: str
    finished_at: str | None = None
    total_ms: int | None = None


class ExplainedGrant(Wire):
    grant_id: str
    role: str
    subject_kind: str
    subject_id: str | None = None
    group_name: str | None = None


class AccessExplained(Wire):
    user_id: str
    environment_id: str
    allowed: bool
    role: str | None = None
    floor: str
    reason: str
    grants: list[ExplainedGrant]
    evaluated_from: str
    published_version: int | None = None


class SecretOut(Wire):
    name: str
    version: str
    live_version: str | None = None
    updated_at: str


class SecretList(Wire):
    environment_id: str
    items: list[SecretOut]


class SecretGrantOut(Wire):
    """Where to PUT a secret's value, straight to the cell. A credential: never print or log it."""

    name: str
    upload: UploadTarget


class SecretSetOut(Wire):
    name: str
    version: str
    changed: bool
    operation_id: str | None = None


class DatabaseOut(Wire):
    """An environment's database as the API knows it. Never a password or a URL."""

    environment_id: str
    present: bool
    database: str | None = None
    connection_limit: int | None = None
    pool_size: int
    size_bytes: int | None = None
    connections: int | None = None
    places_used: int | None = None
    places_total: int | None = None


class LogLineOut(Wire):
    """One redacted line of an app's logs."""

    timestamp: str
    severity: str
    source: str
    text: str


class LogPageOut(Wire):
    environment_id: str
    source: str
    lines: list[LogLineOut]
    cursor: str | None = None


class HealthOut(Wire):
    """Whether an environment runs, sleeps or fails; ``state`` is null when nothing runs."""

    environment_id: str
    state: str | None = None
    reason: str
    last_request_at: str | None = None
    checked_at: str


class UsageOut(Wire):
    """One environment's usage in a month, for metrics only; never a bill."""

    environment_id: str
    month: str
    usage_type: str | None = None
    session_hours: float
    instance_hours: float
    cold_starts: int


class PolicyHost(Wire):
    host: str
    app_id: str
    environment_id: str


class PolicyConnection(Wire):
    """A data connection by name; never its address."""

    name: str
    kind: str
    classification: str


class SubjectDoc(Wire):
    kind: str
    id: str


class CeilingDoc(Wire):
    """The widest audience an app using a connection may have."""

    audience: str
    subjects: list[SubjectDoc] = Field(default_factory=list[SubjectDoc])


class ConnectionOut(Wire):
    """A data connection the caller may see; never its address."""

    name: str
    owner_user_id: str | None
    classification: str
    ceiling: CeilingDoc
    setup_status: str
    status: str


class ConnectionsOut(Wire):
    connections: list[ConnectionOut]


class EnvironmentConnectionOut(Wire):
    environment_id: str
    connection: ConnectionOut
    over_ceiling_since: str | None


class EnvironmentConnectionsOut(Wire):
    connections: list[EnvironmentConnectionOut]


class PolicyApproval(Wire):
    kind: str
    when: str


class PolicyDatabase(Wire):
    places_used: int
    places_total: int
    room: bool


class DeploymentPolicy(Wire):
    """What the org lets the caller's apps reach and use, limited to what the caller may see."""

    scope: str
    hosts: list[PolicyHost]
    connections: list[PolicyConnection]
    approvals: list[PolicyApproval]
    approver: str
    database: PolicyDatabase
    approved_packages: list[str]
    how_to_ask_for_a_package: str


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppCreate(Body):
    slug: str


class GrantIn(Body):
    role: str
    subject_kind: str
    subject_id: str | None = None


class GrantsIn(Body):
    grants: list[GrantIn]


class BundleCreate(Body):
    digest: str
    size_bytes: int
    source_commit: str | None = None


class BuildCreate(Body):
    bundle_id: str


class DeploymentCreate(Body):
    release_id: str
    kind: str
    confirm: bool = Field(default=False, exclude_if=lambda v: not v)


class PromoteIn(Body):
    preview_release_id: str | None = None


class KillSwitchCreate(Body):
    mode: str


class SecretSet(Body):
    version: str
