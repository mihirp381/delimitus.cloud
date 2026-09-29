"""What the CLI reads from and sends to ``/v1``.

Response models ignore fields they do not know, so an additive API change never breaks an older
CLI. Enumerations are plain strings for the same reason. A test checks every field here against
``docs/api/openapi.json``.
"""

from pydantic import BaseModel, ConfigDict


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Whoami(Wire):
    org_id: str
    subject: str
    kind: str
    credential_id: str
    is_agent: bool
    client_id: str | None = None


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


class OperationAccepted(Wire):
    operation_id: str
    state: str


class UploadTarget(Wire):
    """Where to PUT a bundle. ``url`` is a credential: never print or log it."""

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
    created_at: str
    actor: ActorOut


class ReleaseList(Wire):
    items: list[ReleaseOut]
    next_before: int | None = None


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
