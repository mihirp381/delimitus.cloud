"""Which changes need a person's approval, and who may give it (SSC-045, decision 016).

Typed rules, no rule language. Every function is pure. A case the rules do not know raises
:class:`NoRuleError`, so a caller that forgets to handle it fails closed: an exception never
lets a deploy or a share through.
"""

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal, get_args

Profile = Literal["internal"]
PROFILES: Final[frozenset[str]] = frozenset(get_args(Profile))

# (role, subject_kind, subject_id); subject_id is None for an org-wide grant.
type GrantKey = tuple[str, str, str | None]

GateOutcome = Literal["clear", "waiting", "refused"]
DeciderRefusal = Literal["agent_session", "self_approval", "not_eligible"]
ApprovalState = Literal["pending", "approved", "denied", "cancelled"]


class RequirementKind(StrEnum):
    """Mirrors the ``ssc.approval_request.kind`` CHECK."""

    WIDEN_AUDIENCE = "widen_audience"
    CONNECT_DATA_SOURCE = "connect_data_source"
    ENABLE_INTERNET_HOSTS = "enable_internet_hosts"
    AGENT_SHARE = "agent_share"
    EXCEED_CEILING = "exceed_ceiling"


DEPLOY_KINDS: Final = frozenset(
    {RequirementKind.CONNECT_DATA_SOURCE, RequirementKind.ENABLE_INTERNET_HOSTS}
)


class NoRuleError(ValueError):
    """The rules have no answer for this input (unknown profile, capability, state)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True, order=True)
class Requirement:
    """One question a person must answer: ``kind`` about ``subject_key`` in one environment."""

    kind: RequirementKind
    subject_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestedCapabilities:
    """What a release asks to reach. ``unknown`` names capability kinds these rules do not know."""

    connections: frozenset[str] = field(default_factory=frozenset[str])
    egress_hosts: frozenset[str] = field(default_factory=frozenset[str])
    unknown: frozenset[str] = field(default_factory=frozenset[str])


def _profile(profile: str) -> Profile:
    if profile not in PROFILES:
        raise NoRuleError("unknown_profile")
    return "internal"


def required_for_deploy(profile: str, caps: RequestedCapabilities) -> frozenset[Requirement]:
    """Approvals a production deploy needs: one per data connection and one per internet host."""
    match _profile(profile):
        case "internal":
            if caps.unknown:
                raise NoRuleError("unknown_capability")
            return frozenset(
                {Requirement(RequirementKind.CONNECT_DATA_SOURCE, n) for n in caps.connections}
                | {Requirement(RequirementKind.ENABLE_INTERNET_HOSTS, h) for h in caps.egress_hosts}
            )


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def _sorted(grants: Iterable[GrantKey]) -> list[list[str | None]]:
    return [list(g) for g in sorted(grants, key=lambda g: (g[0], g[1], g[2] or ""))]


def share_subject_key(after: Iterable[GrantKey]) -> str:
    """The ``widen_audience`` key: a digest of the whole desired set, in any order."""
    return _digest(_sorted(after))


def agent_share_subject_key(base_version: int, after: Iterable[GrantKey]) -> str:
    """The ``agent_share`` key: the desired set *and* the version it replaces, so an approval
    covers exactly one change and cannot be replayed after the grants move on."""
    return _digest({"grants_version": base_version, "grants": _sorted(after)})


def exceed_subject_key(connection: str, after: Iterable[GrantKey]) -> str:
    """The ``exceed_ceiling`` key: the connection's name and the whole desired set, in any
    order, so an approval covers exactly one audience for one connection."""
    return _digest({"connection": connection, "grants": _sorted(after)})


def widens(before: Collection[GrantKey], after: Collection[GrantKey]) -> bool:
    """A new subject (user, group or the org) gains access, or an org-wide grant is added."""
    subjects_before = {(kind, sid) for _, kind, sid in before}
    new_subject = any((kind, sid) not in subjects_before for _, kind, sid in after)
    new_org_grant = any(g[1] == "org" and g not in before for g in after)
    return new_subject or new_org_grant


def widening_needs_approval(
    profile: str,
    data_connected: bool,
    before: Collection[GrantKey],
    after: Collection[GrantKey],
) -> Requirement | None:
    """Showing a data-connected app to more people needs ``widen_audience`` for the new set."""
    match _profile(profile):
        case "internal":
            if not data_connected or not widens(before, after):
                return None
            return Requirement(RequirementKind.WIDEN_AUDIENCE, share_subject_key(after))


def agent_share_needs_approval(
    via_agent: bool,
    base_version: int,
    before: Collection[GrantKey],
    after: Collection[GrantKey],
) -> Requirement | None:
    """Every grant change made through an agent credential waits for another admin."""
    if not via_agent or set(before) == set(after):
        return None
    return Requirement(RequirementKind.AGENT_SHARE, agent_share_subject_key(base_version, after))


def check_decider(  # noqa: PLR0913  (the owner is keyword-only)
    requester_id: str,
    decider_id: str,
    decider_role: str | None,
    decider_active: bool,
    via_agent: bool,
    *,
    connection_owner_id: str | None = None,
) -> DeciderRefusal | None:
    """Why this person may not decide, or None. Checked in this order: an agent session, then
    self-approval, then eligibility (an active org admin; ``decider_role`` None when unknown).
    For an ``exceed_ceiling`` request the caller passes the connection's owner, who may decide
    too while active."""
    if via_agent:
        return "agent_session"
    if decider_id == requester_id:
        return "self_approval"
    if not decider_active or (decider_role != "admin" and decider_id != connection_owner_id):
        return "not_eligible"
    return None


def gate_outcome(states: Mapping[Requirement, str | None]) -> GateOutcome:
    """Newest state per requirement to an outcome. Any denial refuses; any open or missing
    request waits; only all-approved is clear. A state these rules do not know refuses."""
    known: set[str] = {"pending", "approved", "denied", "cancelled"}
    if any(s is not None and s not in known for s in states.values()):
        return "refused"
    if any(s == "denied" for s in states.values()):
        return "refused"
    if any(s != "approved" for s in states.values()):
        return "waiting"
    return "clear"
