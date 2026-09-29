"""The JSON each command prints under ``--json``.

These shapes are a public contract for scripts and agents (decision 017): fields are only ever
added, never renamed, removed or retyped. ``tests/json_shapes.json`` records every field and a
test fails on any other kind of change.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ssc_cli.doctor.finding import Finding
from ssc_cli.errors import ErrorBody


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


class EnvironmentRow(Shape):
    id: str
    name: str
    config_version: int
    grants_version: int
    current_deployment_id: str | None
    deployment: DeploymentRow | None
    url: str | None = Field(description="Where the environment is served.")


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


class ErrorResult(Shape):
    error: ErrorBody


SHAPES: dict[str, type[BaseModel]] = {
    m.__name__: m
    for m in (
        WhoamiResult,
        TokenSetResult,
        TokenClearResult,
        AppRow,
        AppsResult,
        DeploymentRow,
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
        ErrorBody,
        ErrorResult,
    )
}
