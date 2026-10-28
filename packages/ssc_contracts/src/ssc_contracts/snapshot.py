"""Access snapshot ``ssc-snapshot/v1``: who may reach which app environment of one org.

Frozen contract in ``docs/contracts/access-snapshot.md`` (decision 019). The control plane
compiles and publishes it; ``ssc_shared.access`` evaluates it, in the control plane's explain
endpoint, in the gateway and in the data gateway. A change that would refuse a valid document or
change what one means needs ``ssc-snapshot/v2`` beside this module.

Ids, states and the names admins give hosts and connections only: no people's names, emails or
group names, so a document holds no personal data.
"""

import re
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ssc_contracts.connections import Kind
from ssc_contracts.egress import MAX_CREDENTIALS, MAX_HOSTS, host_pattern_problem
from ssc_contracts.identity import EnvironmentName

FORMAT_V1: Final = "ssc-snapshot/v1"
MAX_VERSION: Final = 2**53 - 1
MAX_SUFFIX: Final = 203
"""The longest apps suffix whose hosts all fit in 253 characters: the longest first label (49)
and a dot take the other 50."""
MAX_PREVIOUS_SUFFIXES: Final = 8
MAX_FORM_TARGETS: Final = 20

OrgId = Annotated[str, StringConstraints(pattern=r"^org_[a-z0-9]{20}$")]
AppId = Annotated[str, StringConstraints(pattern=r"^app_[a-z0-9]{20}$")]
EnvId = Annotated[str, StringConstraints(pattern=r"^env_[a-z0-9]{20}$")]
GrantId = Annotated[str, StringConstraints(pattern=r"^gnt_[a-z0-9]{20}$")]
UserId = Annotated[str, StringConstraints(pattern=r"^usr_[a-z0-9]{20}$")]
GroupId = Annotated[str, StringConstraints(pattern=r"^grp_[a-z0-9]{20}$")]
HostName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")]
ConnectionId = Annotated[str, StringConstraints(pattern=r"^con_[a-z0-9]{20}$")]
ConnectionName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
CredentialId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{12}$")]
Sha1Digest = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+/]{27}=$")]

GrantRole = Literal["builder", "user"]
SubjectKind = Literal["user", "group", "org"]
AppStatus = Literal["active", "disabled", "quarantined"]
UserStatus = Literal["active", "deactivated"]
ConnectionStatus = Literal["active", "suspended"]

_DNS_LABEL: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SUBJECT_ID: Final = {
    "user": re.compile(r"usr_[a-z0-9]{20}"),
    "group": re.compile(r"grp_[a-z0-9]{20}"),
}


def check_suffix(suffix: str) -> str:
    """A lower-case DNS name of at least three labels and at most :data:`MAX_SUFFIX`."""
    labels = suffix.split(".")
    if (
        len(suffix) > MAX_SUFFIX
        or len(labels) < 3  # noqa: PLR2004  (a label under a registrable name)
        or not all(_DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError(f"not a usable apps suffix: {suffix!r}")
    return suffix


Suffix = Annotated[str, AfterValidator(check_suffix)]


def check_form_origin(origin: str) -> str:
    """``https://`` and a lower-case DNS name of at least two labels; no port, path or user."""
    host = origin.removeprefix("https://")
    labels = host.split(".")
    if (
        host == origin
        or len(host) > 253  # noqa: PLR2004  (a DNS name)
        or len(labels) < 2  # noqa: PLR2004  (a name under a top-level domain)
        or not all(_DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError(f"not an https origin: {origin!r}")
    return origin


FormOrigin = Annotated[str, AfterValidator(check_form_origin)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SnapshotEnvironment(_Frozen):
    """One app environment. ``floor`` is the lowest role that grants access to it.
    ``timeout_seconds``: how long one request to it may run (SSC-090). Left out when unset, which
    means the request-billed figure, so documents without it keep their bytes."""

    app_id: AppId
    name: EnvironmentName
    status: AppStatus
    floor: GrantRole
    timeout_seconds: int | None = Field(
        default=None, gt=0, le=MAX_VERSION, exclude_if=lambda v: v is None
    )


class SnapshotGrant(_Frozen):
    """One sharing rule. ``subject_id`` is a ``usr_`` or ``grp_`` id, null for the whole org."""

    grant_id: GrantId
    role: GrantRole
    subject_kind: SubjectKind
    subject_id: str | None

    @model_validator(mode="after")
    def _subject_matches_kind(self) -> Self:
        shape = _SUBJECT_ID.get(self.subject_kind)
        if shape is None:
            if self.subject_id is not None:
                raise ValueError("an org-wide grant has no subject_id")
        elif self.subject_id is None or not shape.fullmatch(self.subject_id):
            raise ValueError(f"a {self.subject_kind} grant needs a {shape.pattern[:4]} id")
        return self


class SnapshotUser(_Frozen):
    """``sessions_not_before``: Unix seconds; a gateway session issued earlier is refused (SSC-019).
    Left out of the document when unset, so documents without it keep their bytes."""

    status: UserStatus
    sessions_not_before: int | None = Field(
        default=None, ge=0, le=MAX_VERSION, exclude_if=lambda v: v is None
    )


def _cap() -> Any:
    return Field(default=None, ge=0, le=MAX_VERSION, exclude_if=lambda v: v is None)


class SnapshotLimits(_Frozen):
    """The caps one layer puts on a data gateway query (SSC-050). A member left out puts no cap
    at this layer; 0 is a cap of zero, so absent and 0 never mean the same."""

    max_rows: int | None = _cap()
    max_bytes: int | None = _cap()
    timeout_ms: int | None = _cap()
    concurrency: int | None = _cap()
    daily_rows: int | None = _cap()
    daily_bytes: int | None = _cap()


class SnapshotConnectionGrant(_Frozen):
    """One environment's use of a connection; ``limits`` are the grant's own caps."""

    limits: SnapshotLimits | None = Field(default=None, exclude_if=lambda v: v is None)


class SnapshotConnection(_Frozen):
    """A company data connection the data gateway serves (SSC-050). ``suspended`` refuses every
    query on it and ends the running ones; ``grants`` lists the environments that may query it."""

    connection_id: ConnectionId
    kind: Kind = Field(default="postgres", exclude_if=lambda v: v == "postgres")
    status: ConnectionStatus
    limits: SnapshotLimits | None = Field(default=None, exclude_if=lambda v: v is None)
    grants: dict[EnvId, SnapshotConnectionGrant]


class SnapshotProxyCredential(_Frozen):
    """One egress proxy credential (SSC-053): its id and base64 SHA-1 of its token, never the
    token. The proxy user is ``<env_id>.<credential_id>``."""

    credential_id: CredentialId
    sha1: Sha1Digest


class SnapshotEgress(_Frozen):
    """The cell egress proxy's policy (SSC-053). ``hosts``: the org's allowlist, host names and
    ``*.`` patterns as ``ssc_contracts.egress`` reads them. ``credentials``: each environment's
    valid proxy credentials, at most two so one can replace the other."""

    hosts: tuple[str, ...] = Field(max_length=MAX_HOSTS)
    credentials: dict[EnvId, tuple[SnapshotProxyCredential, ...]]

    @field_validator("hosts")
    @classmethod
    def _valid_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for host in value:
            if (problem := host_pattern_problem(host)) is not None:
                raise ValueError(f"host {host!r}: {problem}")
        if len(set(value)) != len(value):
            raise ValueError("a host is listed twice")
        if list(value) != sorted(value):
            raise ValueError("hosts must be sorted by byte order")
        return value

    @field_validator("credentials")
    @classmethod
    def _few_credentials(
        cls, value: dict[str, tuple[SnapshotProxyCredential, ...]]
    ) -> dict[str, tuple[SnapshotProxyCredential, ...]]:
        for env_id, held in value.items():
            if not 0 < len(held) <= MAX_CREDENTIALS:
                raise ValueError(f"{env_id} holds 1 to {MAX_CREDENTIALS} credentials")
            if len({c.credential_id for c in held}) != len(held):
                raise ValueError(f"{env_id} lists a credential twice")
        return value


class SnapshotRetiredSuffix(_Frozen):
    """A suffix the org used before, recognised until ``until``."""

    suffix: Suffix
    until: AwareDatetime


class SnapshotDoc(_Frozen):
    """One org's snapshot. ``version`` 0 is a live evaluation that was never published."""

    format: Literal["ssc-snapshot/v1"]
    org_id: OrgId
    version: int = Field(ge=0, le=MAX_VERSION)
    compiled_at: AwareDatetime
    environments: dict[EnvId, SnapshotEnvironment]
    hosts: dict[HostName, EnvId] = Field(
        description="First host label (decision 004) to environment; the gateway's lookup."
    )
    grants: dict[EnvId, tuple[SnapshotGrant, ...]]
    groups_by_user: dict[UserId, tuple[GroupId, ...]]
    users: dict[UserId, SnapshotUser]
    ceiling: None
    connections: dict[ConnectionName, SnapshotConnection] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Connection name to the connection the data gateway serves (SSC-050).",
    )
    egress: SnapshotEgress | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="The egress proxy's allowlist and credentials (SSC-053).",
    )
    suffix: Suffix | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="The org's apps suffix (CD-1); absent means <cell label>.<apps domain>.",
    )
    previous_suffixes: tuple[SnapshotRetiredSuffix, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PREVIOUS_SUFFIXES,
        exclude_if=lambda v: v is None,
        description="Suffixes the org used before, by until then suffix (CD-1).",
    )
    form_targets: dict[EnvId, tuple[FormOrigin, ...]] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Origins each environment's forms may post to beside its own (CD-5.2).",
    )

    @field_validator("form_targets")
    @classmethod
    def _few_form_targets(
        cls, value: dict[str, tuple[str, ...]] | None
    ) -> dict[str, tuple[str, ...]] | None:
        for env_id, origins in (value or {}).items():
            if not 0 < len(origins) <= MAX_FORM_TARGETS:
                raise ValueError(f"{env_id} lists 1 to {MAX_FORM_TARGETS} form targets")
            if list(origins) != sorted(set(origins)):
                raise ValueError(f"{env_id} form targets must be unique and sorted by byte order")
        return value

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
        connections = (self.connections or {}).values()
        if any(not c.grants.keys() <= self.environments.keys() for c in connections):
            raise ValueError("a connection grant names an environment that is not in environments")
        ids = [c.connection_id for c in connections]
        if len(ids) != len(set(ids)):
            raise ValueError("two connection names share a connection_id")
        if (
            self.egress is not None
            and not self.egress.credentials.keys() <= self.environments.keys()
        ):
            raise ValueError(
                "an egress credential names an environment that is not in environments"
            )
        if not (self.form_targets or {}).keys() <= self.environments.keys():
            raise ValueError("form_targets name an environment that is not in environments")
        previous = self.previous_suffixes or ()
        keys = [(p.until, p.suffix) for p in previous]
        if keys != sorted(keys):
            raise ValueError("previous_suffixes must be sorted by until, then suffix")
        names = {p.suffix for p in previous}
        if len(names) != len(previous) or self.suffix in names:
            raise ValueError("a suffix is listed twice")
        return self
