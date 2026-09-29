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


class ShareResult(Shape):
    """``share`` and ``unshare``: the sharing rules after the change. When ``pending`` names
    approval requests, nothing changed yet and ``grants`` are the rules still in force."""

    app_id: str
    environment: str
    environment_id: str
    grants_version: int
    changed: bool
    grants: list[GrantRow]
    pending: list[str] = Field(default_factory=list[str])


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
        ShareResult,
        Finding,
        DoctorResult,
        FileAction,
        InitResult,
        ErrorBody,
        ErrorResult,
    )
}
