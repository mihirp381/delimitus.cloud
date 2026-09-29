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


class OperationOut(Wire):
    operation_id: str
    kind: str
    state: str
    app_id: str
    environment_id: str
    release_id: str | None = None
    started_at: str
    finished_at: str | None = None


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
