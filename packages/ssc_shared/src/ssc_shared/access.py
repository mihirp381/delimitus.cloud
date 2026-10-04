"""The one access evaluator: may this user reach this app environment? (SSC-021, decision 019)

The control plane's explain endpoint and the gateway (SSC-018) both call :func:`decide` on an
:class:`AccessView` built from an ``ssc-snapshot/v1`` document; nothing else decides access.
There is no owner or org-admin shortcut: everyone, the app's owner included, needs a grant.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import ValidationError

from ssc_contracts.identity import EnvironmentName
from ssc_contracts.snapshot import (
    GrantRole,
    SnapshotConnection,
    SnapshotDoc,
    SnapshotEgress,
    SubjectKind,
)
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS

Reason = Literal[
    "no_view",
    "unknown_environment",
    "app_not_active",
    "user_not_active",
    "no_grant",
    "below_floor",
    "granted",
]

RANK: Final[Mapping[str, int]] = MappingProxyType({"user": 1, "builder": 2})


class SnapshotInvalidError(ValueError):
    """A document that is not a valid ``ssc-snapshot/v1``, or not for this holder's org."""


@dataclass(frozen=True, slots=True)
class GrantRef:
    grant_id: str
    role: GrantRole
    subject_kind: SubjectKind
    subject_id: str | None


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """``role`` is the best role granted when ``allowed``; ``via`` the grants that count."""

    allowed: bool
    role: GrantRole | None
    via: tuple[GrantRef, ...]
    reason: Reason


@dataclass(frozen=True, slots=True)
class EnvironmentIndex:
    """One environment's grants, split by subject for lookup. ``timeout_seconds`` is the
    document's, or ``REQUEST_TIMEOUT_SECONDS`` when it has none."""

    app_id: str
    name: EnvironmentName
    active: bool
    floor: GrantRole
    org_wide: tuple[GrantRef, ...]
    by_user: Mapping[str, tuple[GrantRef, ...]]
    by_group: Mapping[str, tuple[GrantRef, ...]]
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS


def _index(doc: SnapshotDoc, env_id: str) -> EnvironmentIndex:
    env = doc.environments[env_id]
    org_wide: list[GrantRef] = []
    by_user: dict[str, list[GrantRef]] = {}
    by_group: dict[str, list[GrantRef]] = {}
    for g in doc.grants.get(env_id, ()):
        ref = GrantRef(g.grant_id, g.role, g.subject_kind, g.subject_id)
        if g.subject_id is None:
            org_wide.append(ref)
        else:
            target = by_user if g.subject_kind == "user" else by_group
            target.setdefault(g.subject_id, []).append(ref)
    return EnvironmentIndex(
        app_id=env.app_id,
        name=env.name,
        active=env.status == "active",
        floor=env.floor,
        org_wide=tuple(org_wide),
        by_user=MappingProxyType({k: tuple(v) for k, v in by_user.items()}),
        by_group=MappingProxyType({k: tuple(v) for k, v in by_group.items()}),
        timeout_seconds=env.timeout_seconds or REQUEST_TIMEOUT_SECONDS,
    )


class AccessView:
    """A validated snapshot with its lookups built. Immutable once constructed. ``connections``
    is the data gateway's lookup (SSC-050): name to connection, empty when the document has none.
    ``egress`` is the egress proxy's allowlist and credentials (SSC-053), None when it has none."""

    __slots__ = (
        "active_users",
        "compiled_at",
        "connections",
        "egress",
        "environments",
        "groups_by_user",
        "hosts",
        "not_before",
        "org_id",
        "version",
    )

    def __init__(self, doc: SnapshotDoc) -> None:
        self.org_id: str = doc.org_id
        self.version: int = doc.version
        self.compiled_at: datetime = doc.compiled_at
        self.environments: Mapping[str, EnvironmentIndex] = MappingProxyType(
            {env_id: _index(doc, env_id) for env_id in doc.environments}
        )
        self.active_users: frozenset[str] = frozenset(
            u for u, info in doc.users.items() if info.status == "active"
        )
        self.groups_by_user: Mapping[str, frozenset[str]] = MappingProxyType(
            {u: frozenset(gs) for u, gs in doc.groups_by_user.items()}
        )
        self.hosts: Mapping[str, str] = MappingProxyType(dict(doc.hosts))
        self.not_before: Mapping[str, int] = MappingProxyType(
            {
                u: info.sessions_not_before
                for u, info in doc.users.items()
                if info.sessions_not_before is not None
            }
        )
        self.connections: Mapping[str, SnapshotConnection] = MappingProxyType(
            dict(doc.connections or {})
        )
        self.egress: SnapshotEgress | None = doc.egress

    @classmethod
    def from_document(cls, doc: SnapshotDoc | Mapping[str, object] | bytes | str) -> Self:
        """Validate ``doc`` and build the view; :class:`SnapshotInvalidError` if it is invalid."""
        try:
            if isinstance(doc, SnapshotDoc):
                parsed = doc
            elif isinstance(doc, (bytes, str)):
                parsed = SnapshotDoc.model_validate_json(doc)
            else:
                parsed = SnapshotDoc.model_validate(doc)
            return cls(parsed)
        except (ValidationError, ValueError, TypeError, RecursionError) as exc:
            raise SnapshotInvalidError(str(exc)) from exc

    def floor_of(self, environment_id: str) -> GrantRole | None:
        env = self.environments.get(environment_id)
        return None if env is None else env.floor


def _matching(env: EnvironmentIndex, user_id: str, groups: frozenset[str]) -> tuple[GrantRef, ...]:
    found = [*env.org_wide, *env.by_user.get(user_id, ())]
    for group_id in sorted(groups):
        found.extend(env.by_group.get(group_id, ()))
    return tuple(sorted(found, key=lambda g: g.grant_id))


def _refusal(view: AccessView | None, environment_id: str, user_id: str) -> Reason | None:
    if view is None:
        return "no_view"
    env = view.environments.get(environment_id)
    if env is None:
        return "unknown_environment"
    if not env.active:
        return "app_not_active"
    if user_id not in view.active_users:
        return "user_not_active"
    return None


def decide(view: AccessView | None, environment_id: str, user_id: str) -> AccessDecision:
    """The access decision for ``user_id`` on ``environment_id``. Never raises."""
    refused = _refusal(view, environment_id, user_id)
    if refused is not None or view is None:
        return AccessDecision(allowed=False, role=None, via=(), reason=refused or "no_view")
    env = view.environments[environment_id]
    matching = _matching(env, user_id, view.groups_by_user.get(user_id, frozenset()))
    if not matching:
        return AccessDecision(allowed=False, role=None, via=(), reason="no_grant")
    floor = RANK[env.floor]
    counted = tuple(g for g in matching if RANK[g.role] >= floor)
    if not counted:
        return AccessDecision(allowed=False, role=None, via=matching, reason="below_floor")
    best = max(counted, key=lambda g: RANK[g.role]).role
    return AccessDecision(allowed=True, role=best, via=counted, reason="granted")


class ViewHolder:
    """The current view for one org. :meth:`apply` swaps in a newer one or keeps the old."""

    __slots__ = ("_org_id", "_view")

    def __init__(self, org_id: str) -> None:
        self._org_id = org_id
        self._view: AccessView | None = None

    @property
    def org_id(self) -> str:
        return self._org_id

    @property
    def view(self) -> AccessView | None:
        return self._view

    def apply(self, doc: SnapshotDoc | Mapping[str, object] | bytes | str) -> bool:
        """Build the new view first; swap only if it is valid, this org's, and newer.

        Raises :class:`SnapshotInvalidError` for an invalid, unpublished or foreign document and
        keeps the current view. Returns False for a version that is not newer."""
        new = AccessView.from_document(doc)
        if new.org_id != self._org_id:
            raise SnapshotInvalidError(f"snapshot is for {new.org_id}, not {self._org_id}")
        if new.version < 1:
            raise SnapshotInvalidError("version 0 is a live evaluation, never published")
        current = self._view
        if current is not None and new.version <= current.version:
            return False
        self._view = new
        return True
