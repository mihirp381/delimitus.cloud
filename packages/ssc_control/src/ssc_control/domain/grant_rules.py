"""Sharing rules (SSC-021, decision 019): the role floor of each environment, and the hook for
the audience ceiling (SSC-052). Pure functions over grant keys; the API maps the problems.

The floor is the lowest role that grants access: anyone may be given ``user`` in prod, but
preview is for builders, so a preview ``user`` grant would grant nothing and is refused.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from ssc_control.domain.approval_rules import GrantKey

Role = Literal["builder", "user"]
FLOOR: Final[Mapping[str, Role]] = MappingProxyType({"prod": "user", "preview": "builder"})
RANK: Final[Mapping[str, int]] = MappingProxyType({"user": 1, "builder": 2})

Problem = Literal["below_floor", "duplicate_subject"]


@dataclass(frozen=True, slots=True)
class GrantProblem:
    problem: Problem
    grant: GrantKey


@dataclass(frozen=True, slots=True)
class SharingTarget:
    """The environment a change is for, as the ceiling hook sees it."""

    environment_id: str
    name: str
    profile: str


def floor_of(env_name: str) -> Role:
    """The floor for ``env_name``; ``KeyError`` for a name the rules do not know."""
    return FLOOR[env_name]


def validate(env_name: str, grants: Iterable[GrantKey]) -> tuple[GrantProblem, ...]:
    """Grants below the environment's floor, and second grants for one subject (the database
    keeps one grant per subject per environment, whatever its role)."""
    floor = RANK[floor_of(env_name)]
    problems: list[GrantProblem] = []
    seen: set[tuple[str, str | None]] = set()
    for key in grants:
        role, kind, subject = key
        if RANK[role] < floor:
            problems.append(GrantProblem("below_floor", key))
        if (kind, subject) in seen:
            problems.append(GrantProblem("duplicate_subject", key))
        seen.add((kind, subject))
    return tuple(problems)


def audience_ceiling(target: SharingTarget, desired: Collection[GrantKey]) -> None:
    """The widest audience ``target`` may have. No ceiling yet (SSC-052): accepts everything."""
    del target, desired
