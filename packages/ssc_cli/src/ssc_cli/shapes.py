"""The JSON each command prints under ``--json``.

These shapes are a public contract for scripts and agents (decision 017): fields are only ever
added, never renamed, removed or retyped. ``tests/json_shapes.json`` records every field and a
test fails on any other kind of change.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ssc_cli.doctor.finding import Finding
from ssc_cli.errors import ErrorBody
from ssc_shared.requirements import PlatformRequirements, PlatformRule, ResourceSize


class Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WhoamiResult(Shape):
    api_url: str
    org_id: str
    subject: str
    kind: str
    credential_id: str
    is_agent: bool
    client_id: str | None
    role: str | None


class TokenSetResult(Shape):
    api_url: str
    stored_in: Literal["keychain"]
    org_id: str
    subject: str


class TokenClearResult(Shape):
    api_url: str
    cleared: bool


class LoginResult(Shape):
    api_url: str
    auth_url: str
    org_id: str
    subject: str
    stored_in: Literal["keychain"]
    agent: str | None = None


class LogoutResult(Shape):
    api_url: str
    revoked: bool
    cleared: bool
    agent: bool = False


class AppRow(Shape):
    id: str
    slug: str
    owner_user_id: str
    status: str


class AppsResult(Shape):
    apps: list[AppRow]


class DeploymentRow(Shape):
    operation_id: str
    kind: str
    state: str
    release_id: str | None
    started_at: str
    finished_at: str | None
    billing: Literal["request", "instance"] | None = Field(
        default=None,
        description="Only from ``status``: ``instance`` for a session app, billed while its one "
        "instance runs; ``request`` for any other, billed while it answers; null when the API "
        "did not say.",
    )


class DatabaseRow(Shape):
    """``status``: the environment's database, when it has one. Never a password or a URL.
    ``size_bytes``, ``connections`` and the places are null when the cell did not say."""

    database: str
    connection_limit: int | None
    pool_size: int
    size_bytes: int | None
    connections: int | None
    places_used: int | None
    places_total: int | None
    tier: str | None = Field(
        default=None, description="The instance's tier, such as db-f1-micro; null when unknown."
    )


class HealthRow(Shape):
    """``status``: ``running``, ``asleep`` (starts on the next request) or ``failing``; ``state``
    is null when nothing runs or the cell cannot tell, and ``reason`` says which."""

    state: str | None
    reason: str
    last_request_at: str | None
    checked_at: str


class UsageRow(Shape):
    """``status``: this month's usage (UTC), for metrics only and never a bill. ``usage_type`` is
    ``rare``, ``daily``, ``session`` or ``heavy``, read from the month's usage; null with none."""

    month: str
    usage_type: str | None
    session_hours: float
    instance_hours: float
    cold_starts: int


class EnvironmentRow(Shape):
    id: str
    name: str
    config_version: int
    grants_version: int
    current_deployment_id: str | None
    deployment: DeploymentRow | None
    url: str | None = Field(description="Where the environment is served.")
    database: DatabaseRow | None = Field(
        default=None, description="Only from ``status``; null when there is none."
    )
    health: HealthRow | None = Field(
        default=None, description="Only from ``status``; null when the API did not say."
    )
    usage: UsageRow | None = Field(
        default=None, description="Only from ``status``; null when the API did not say."
    )


class AppResult(Shape):
    """``apps create`` and ``status``."""

    id: str
    slug: str
    owner_user_id: str
    status: str
    created_at: str
    environments: list[EnvironmentRow]


class GrantRow(Shape):
    id: str
    role: str
    subject_kind: str
    subject_id: str | None


class SubjectRow(Shape):
    kind: str
    id: str | None


class ShareResult(Shape):
    """``share`` and ``unshare``: the sharing rules after the change. When ``pending`` names
    approval requests, nothing changed yet and ``grants`` are the rules still in force.
    ``subject_kind`` and ``subject_id`` say whose grant it was, after an email or group name was
    looked up; ``subjects`` lists every subject named, since ``unshare`` takes several."""

    app_id: str
    environment: str
    environment_id: str
    grants_version: int
    changed: bool
    grants: list[GrantRow]
    pending: list[str] = Field(default_factory=list[str])
    subject_kind: str
    subject_id: str | None
    subjects: list[SubjectRow] = Field(default_factory=list[SubjectRow])


class DoctorResult(Shape):
    path: str
    blocking: bool
    findings: list[Finding]


class FileAction(Shape):
    path: str
    action: Literal["created", "updated", "unchanged", "skipped"]
    note: str | None


class InitResult(Shape):
    path: str
    files: list[FileAction]


class BundleWarning(Shape):
    """A secret-scan finding that does not block, with the value masked."""

    path: str
    line: int
    rule: str
    masked: str


class CapabilityChangeRow(Shape):
    severity: str
    kind: str
    subject: str
    consequence: str
    approver: str | None


class DeployResult(Shape):
    """``deploy``: always to preview. ``state`` is the deployment's, ``pending`` until it is
    live unless ``--wait`` was given. A failed build or deployment prints an error instead."""

    app_id: str
    slug: str
    environment: Literal["preview"]
    environment_id: str
    bundle_id: str
    digest: str
    uploaded: bool = Field(description="False when the API already had these exact bytes.")
    build_id: str
    release_id: str
    release_number: int
    operation_id: str
    state: str
    url: str | None
    warnings: list[BundleWarning]
    capability_changes: list[CapabilityChangeRow]
    notice: str | None = Field(
        default=None,
        description="What the deployment sets off, such as the company's database being "
        "created the first time; null when nothing.",
    )


class ReleaseRow(Shape):
    release_id: str
    number: int
    label: str
    built_for: str | None = Field(description="The environment a build made it for.")
    built_for_environment_id: str | None
    live_in: list[str] = Field(description="Environments whose live deployment runs it.")
    source_digest: str
    source_commit: str | None
    image_digest: str
    created_at: str
    actor_kind: str
    actor_id: str
    via_agent: bool


class ReleasesResult(Shape):
    app_id: str
    slug: str
    releases: list[ReleaseRow] = Field(description="Highest number first.")
    next_before: int | None = Field(description="Pass as `--before` for the next page.")


class RollbackResult(Shape):
    """``rollback``: ``state`` is the deployment's, ``pending`` unless ``--wait`` was given."""

    app_id: str
    slug: str
    environment: str
    environment_id: str
    release_id: str
    release_number: int
    operation_id: str
    state: str
    url: str | None


class PromoteResult(Shape):
    """``promote``: prod builds what preview runs, then deploys it. Without ``--wait`` or
    ``--build`` the command stops once the build has made its release: ``operation_id`` and
    ``state`` are null and ``next_command`` puts the release live. ``source_release_id`` is null
    with ``--build``."""

    app_id: str
    slug: str
    environment: Literal["prod"]
    environment_id: str
    source_release_id: str | None
    build_id: str
    release_id: str
    release_number: int
    operation_id: str | None
    state: str | None
    url: str | None
    next_command: str | None


class KillSwitchStepRow(Shape):
    name: str
    state: str
    elapsed_ms: int | None
    attempts: int
    error: str | None


class DisableResult(Shape):
    """``disable``: the app's new ``status`` holds from the moment the API answered; ``state`` is
    the kill switch run's once ssc stopped following it."""

    app_id: str
    slug: str
    mode: str
    status: str
    run_id: str
    state: str
    steps: list[KillSwitchStepRow]
    total_ms: int | None


class AccessGrantRow(Shape):
    grant_id: str
    role: str
    subject_kind: str
    subject_id: str | None
    group_name: str | None


class AccessResult(Shape):
    """``access explain``: whether ``user_id`` can open the environment, and the grants that
    decided it."""

    app_id: str
    slug: str
    environment: str
    environment_id: str
    user_id: str
    allowed: bool
    role: str | None
    floor: str
    reason: str
    grants: list[AccessGrantRow]
    evaluated_from: str
    published_version: int | None


class SecretSetResult(Shape):
    """``secret set``: the version now recorded, never the value. ``operation_id`` is the
    deployment that puts it live (null when nothing changed or nothing is live yet); ``state`` is
    that deployment's, ``pending`` unless ``--wait`` was given."""

    app_id: str
    slug: str
    environment: str
    environment_id: str
    name: str
    version: str
    changed: bool
    operation_id: str | None
    state: str | None


class DatabaseRotateResult(Shape):
    """``database rotate``: when the password was rotated, never the password. ``operation_id`` is
    the deployment that puts it live (null when nothing is live); ``state`` is that deployment's,
    ``pending`` unless ``--wait`` was given, and null when there is none."""

    app_id: str
    slug: str
    environment: str
    environment_id: str
    rotated_at: str
    operation_id: str | None
    state: str | None


class SecretRow(Shape):
    name: str
    version: str
    live_version: str | None
    updated_at: str


class SecretsResult(Shape):
    """``secret list``: names and versions only."""

    app_id: str
    slug: str
    environment: str
    environment_id: str
    secrets: list[SecretRow]


class LogLineRow(Shape):
    """``logs``: one redacted line. ``logs --follow --json`` prints one per line as it comes."""

    timestamp: str
    severity: str
    source: str
    text: str


class LogsResult(Shape):
    """``logs``: the newest lines, oldest first. ``cursor`` continues after the last of them."""

    app_id: str
    slug: str
    environment: str
    environment_id: str
    source: str
    lines: list[LogLineRow]
    cursor: str | None


class PolicyHostRow(Shape):
    host: str
    app_id: str
    environment_id: str


class PolicyConnectionRow(Shape):
    name: str
    kind: str
    classification: str


class PolicyApprovalRow(Shape):
    kind: str
    when: str


class PolicyDatabaseRow(Shape):
    places_used: int
    places_total: int
    room: bool


class PolicyResult(Shape):
    """``policy``: what the org lets the caller's apps reach and use. ``scope`` is ``org`` for an
    org admin, else ``own``: only what the caller's own approved requests opened."""

    api_url: str
    scope: str
    hosts: list[PolicyHostRow]
    connections: list[PolicyConnectionRow]
    approvals: list[PolicyApprovalRow]
    approver: str
    database: PolicyDatabaseRow
    approved_packages: list[str]
    how_to_ask_for_a_package: str


class ConnectionRow(Shape):
    """One data connection. ``ceiling`` is ``org`` or ``group:<id>`` and ``user:<id>`` entries;
    ``over_ceiling_since`` is set for an environment's connection whose audience is now wider
    than the ceiling."""

    name: str
    classification: str
    ceiling: list[str]
    setup_status: str
    status: str
    over_ceiling_since: str | None


class ConnectionsResult(Shape):
    """``connections``: the org's connections the caller may see, or with an app the ones one of
    its environments may reach (``app_id``, ``slug`` and ``environment`` are then set)."""

    api_url: str
    app_id: str | None
    slug: str | None
    environment: str | None
    connections: list[ConnectionRow]


class ApprovalRow(Shape):
    """One approval request. ``subject`` is the host or data source asked for; null for a change
    to sharing, whose rules ``ssc approvals show`` lists."""

    id: str
    app: str
    environment: str
    kind: str
    subject: str | None
    state: str
    requested_by: str
    requested_via_agent: bool
    created_at: str


class ApprovalsResult(Shape):
    """``approvals list``: what you may decide (``scope`` ``inbox``) or everything you may see
    (``all``), newest first. ``more`` is true when older ones were left out."""

    api_url: str
    scope: Literal["inbox", "all"]
    approvals: list[ApprovalRow]
    more: bool


class ApprovalGrantRow(Shape):
    role: str
    subject_kind: str
    subject_id: str | None
    subject_name: str | None


class ApprovalShowResult(Shape):
    """``approvals show``: one request with what it would change. ``added`` and ``removed`` are
    the sharing rules against those in force now; both are empty for other kinds."""

    api_url: str
    id: str
    app: str
    environment: str
    kind: str
    subject: str | None
    state: str
    requested_by: str
    requested_via_agent: bool
    decided_by_user_id: str | None
    decision_reason: str | None
    created_at: str
    added: list[ApprovalGrantRow]
    removed: list[ApprovalGrantRow]
    connection: str | None
    can_decide: bool


class ApprovalDecisionResult(Shape):
    """``approvals approve`` and ``approvals reject``. ``applied`` says what approving did:
    ``applied``, ``waiting`` (another approval is still open), ``not_applied`` (the change no
    longer fits; ``applied_reason`` says why) or ``not_applicable``."""

    api_url: str
    id: str
    state: str
    reason: str
    applied: str
    applied_reason: str | None


class ErrorResult(Shape):
    error: ErrorBody


SHAPES: dict[str, type[BaseModel]] = {
    m.__name__: m
    for m in (
        WhoamiResult,
        TokenSetResult,
        TokenClearResult,
        LoginResult,
        LogoutResult,
        AppRow,
        AppsResult,
        DeploymentRow,
        DatabaseRow,
        HealthRow,
        UsageRow,
        EnvironmentRow,
        AppResult,
        GrantRow,
        SubjectRow,
        ShareResult,
        Finding,
        DoctorResult,
        FileAction,
        InitResult,
        BundleWarning,
        CapabilityChangeRow,
        DeployResult,
        ReleaseRow,
        ReleasesResult,
        RollbackResult,
        PromoteResult,
        KillSwitchStepRow,
        DisableResult,
        AccessGrantRow,
        AccessResult,
        SecretSetResult,
        SecretRow,
        SecretsResult,
        DatabaseRotateResult,
        LogLineRow,
        LogsResult,
        ErrorBody,
        ErrorResult,
        PlatformRule,
        ResourceSize,
        PlatformRequirements,
        PolicyHostRow,
        PolicyConnectionRow,
        PolicyApprovalRow,
        PolicyDatabaseRow,
        PolicyResult,
        ConnectionRow,
        ConnectionsResult,
        ApprovalRow,
        ApprovalsResult,
        ApprovalGrantRow,
        ApprovalShowResult,
        ApprovalDecisionResult,
    )
}
