"""Access snapshot ``ssc-snapshot/v1``: who may reach which app environment of one org.

Frozen contract in ``docs/contracts/access-snapshot.md`` (decision 019). The control plane
compiles and publishes it; ``ssc_shared.access`` evaluates it, in the control plane's explain
endpoint and in the gateway. A change that would refuse a valid document or change what one
means needs ``ssc-snapshot/v2`` beside this module.

Ids and states only: no names, emails or group names, so a document holds no personal data.
"""

from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ssc_contracts.identity import EnvironmentName

FORMAT_V1: Final = "ssc-snapshot/v1"
MAX_VERSION: Final = 2**53 - 1

OrgId = Annotated[str, StringConstraints(pattern=r"^org_[a-z0-9]{20}$")]
AppId = Annotated[str, StringConstraints(pattern=r"^app_[a-z0-9]{20}$")]
EnvId = Annotated[str, StringConstraints(pattern=r"^env_[a-z0-9]{20}$")]
GrantId = Annotated[str, StringConstraints(pattern=r"^gnt_[a-z0-9]{20}$")]
UserId = Annotated[str, StringConstraints(pattern=r"^usr_[a-z0-9]{20}$")]
GroupId = Annotated[str, StringConstraints(pattern=r"^grp_[a-z0-9]{20}$")]
HostName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")]

GrantRole = Literal["builder", "user"]
SubjectKind = Literal["user", "group", "org"]
AppStatus = Literal["active", "disabled", "quarantined"]
UserStatus = Literal["active", "deactivated"]

_SUBJECT_PREFIX: Final = {"user": "usr_", "group": "grp_"}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SnapshotEnvironment(_Frozen):
    """One app environment. ``floor`` is the lowest role that grants access to it."""

    app_id: AppId
    name: EnvironmentName
    status: AppStatus
    floor: GrantRole


class SnapshotGrant(_Frozen):
    """One sharing rule. ``subject_id`` is a ``usr_`` or ``grp_`` id, null for the whole org."""

    grant_id: GrantId
    role: GrantRole
    subject_kind: SubjectKind
    subject_id: str | None

    @model_validator(mode="after")
    def _subject_matches_kind(self) -> Self:
        prefix = _SUBJECT_PREFIX.get(self.subject_kind)
        if prefix is None:
            if self.subject_id is not None:
                raise ValueError("an org-wide grant has no subject_id")
        elif self.subject_id is None or not self.subject_id.startswith(prefix):
            raise ValueError(f"a {self.subject_kind} grant needs a {prefix} subject_id")
        return self


class SnapshotUser(_Frozen):
    status: UserStatus


class SnapshotDoc(_Frozen):
    """One org's snapshot. ``version`` 0 is a live evaluation that was never published."""

    format: Literal["ssc-snapshot/v1"]
    org_id: OrgId
    version: int = Field(ge=0, le=MAX_VERSION)
    compiled_at: AwareDatetime
    environments: dict[EnvId, SnapshotEnvironment]
    hosts: dict[HostName, EnvId] = Field(description="Empty until host labels exist (SSC-013).")
    grants: dict[EnvId, tuple[SnapshotGrant, ...]]
    groups_by_user: dict[UserId, tuple[GroupId, ...]]
    users: dict[UserId, SnapshotUser]
    ceiling: None

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        if not self.grants.keys() <= self.environments.keys():
            raise ValueError("grants name an environment that is not in environments")
        if not set(self.hosts.values()) <= self.environments.keys():
            raise ValueError("hosts name an environment that is not in environments")
        if not self.groups_by_user.keys() <= self.users.keys():
            raise ValueError("groups_by_user names a user that is not in users")
        for grants in self.grants.values():
            for g in grants:
                if g.subject_kind == "user" and g.subject_id not in self.users:
                    raise ValueError(f"grant {g.grant_id} names a user that is not in users")
        return self
