"""What a manifest asks for that the environment does not grant: a ranked diff that never blocks.

A manifest is a request. The deploy goes ahead and the builder sees this diff; the platform
decides each item separately. Ranking and cap follow the consequence-ranked diff pattern.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from ssc_contracts.egress import pattern_matches
from ssc_contracts.manifest import Manifest

Severity = Literal["high", "medium", "low"]
ChangeKind = Literal[
    "postgres_missing", "connection_missing", "egress_host_missing", "schedules_declared"
]
SEVERITY_RANK: Final[Mapping[Severity, int]] = MappingProxyType({"high": 0, "medium": 1, "low": 2})
MAX_LISTED_CHANGES: Final = 20
ORG_ADMIN: Final = "org admin"

_CONSEQUENCE: Final[Mapping[ChangeKind, str]] = MappingProxyType(
    {
        "postgres_missing": "No Postgres here yet: DATABASE_URL is unset and database calls fail.",
        "connection_missing": "Not granted here: calls through it are refused until it is granted.",
        "egress_host_missing": "Outbound calls to this host are blocked until it is allowed.",
        "schedules_declared": "This timer calls the app after deploy; preview timers start paused.",
    }
)
_SEVERITY: Final[Mapping[ChangeKind, Severity]] = MappingProxyType(
    {
        "postgres_missing": "high",
        "connection_missing": "high",
        "egress_host_missing": "medium",
        "schedules_declared": "low",
    }
)
_APPROVER: Final[Mapping[ChangeKind, str | None]] = MappingProxyType(
    {
        "postgres_missing": None,
        "connection_missing": ORG_ADMIN,
        "egress_host_missing": ORG_ADMIN,
        "schedules_declared": None,
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnvironmentCapabilities(_Frozen):
    """What one environment grants today. ``egress_hosts`` are allowlist patterns
    (``ssc_contracts.egress``): ``*.example.com`` grants ``api.example.com``."""

    postgres: bool = False
    connections: frozenset[str] = frozenset()
    egress_hosts: frozenset[str] = frozenset()


class CapabilityChange(_Frozen):
    """One thing asked for and not granted. ``approver`` is None when no approval step applies."""

    severity: Severity
    kind: ChangeKind
    subject: str
    consequence: str
    approver: str | None


class CapabilityDiff(_Frozen):
    """The listed changes (at most ``MAX_LISTED_CHANGES``, highest first) and the full count."""

    changes: tuple[CapabilityChange, ...] = Field(max_length=MAX_LISTED_CHANGES)
    total: int = Field(ge=0)
    blocks: Literal[False] = False

    @computed_field
    @property
    def summarised(self) -> bool:
        return self.total > len(self.changes)


def _change(kind: ChangeKind, subject: str) -> CapabilityChange:
    return CapabilityChange(
        severity=_SEVERITY[kind],
        kind=kind,
        subject=subject,
        consequence=_CONSEQUENCE[kind],
        approver=_APPROVER[kind],
    )


def diff_capabilities(manifest: Manifest, caps: EnvironmentCapabilities) -> CapabilityDiff:
    """Rank what ``manifest`` asks for beyond ``caps``: severity, then kind, then subject."""
    found: list[CapabilityChange] = []
    if manifest.state.postgres and not caps.postgres:
        found.append(_change("postgres_missing", "postgres"))
    found += [
        _change("connection_missing", name)
        for name in manifest.connections.names
        if name not in caps.connections
    ]
    found += [
        _change("egress_host_missing", host)
        for host in manifest.egress.hosts
        if not any(pattern_matches(p, host) for p in caps.egress_hosts)
    ]
    found += [_change("schedules_declared", s.name) for s in manifest.schedules]
    found.sort(key=lambda c: (SEVERITY_RANK[c.severity], c.kind, c.subject))
    return CapabilityDiff(changes=tuple(found[:MAX_LISTED_CHANGES]), total=len(found))


def render_diff(diff: CapabilityDiff) -> str:
    """Plain text for the CLI and build log."""
    if not diff.changes:
        return "No change: this environment grants everything the manifest asks for."
    lines = ["The manifest asks for more than this environment grants; the deploy continues."]
    for c in diff.changes:
        approver = f" Approver: {c.approver}." if c.approver else ""
        lines.append(f"[{c.severity.upper()}] {c.kind} {c.subject}: {c.consequence}{approver}")
    if diff.summarised:
        lines.append(f"... and {diff.total - len(diff.changes)} more changes, not listed.")
    return "\n".join(lines)
