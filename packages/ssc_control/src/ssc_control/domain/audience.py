"""The audience ceiling of a data connection (SSC-052): the widest audience an app that uses it
may have. Pure functions over grant keys.

A ceiling is the whole org, or a listed set of groups and users. A grant is inside a listed set
when its subject is listed. A user is also inside when they are an active member of a listed
group at the moment of the check; the service adds those users with :func:`with_members`, so
this module reads no directory. An org-wide grant is inside only the whole-org ceiling.
"""

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from ssc_control.domain.approval_rules import GrantKey

type Subject = tuple[str, str]

MAX_SUBJECTS: Final = 100
_ID: Final = {
    "group": re.compile(r"^grp_[a-z0-9]{20}$"),
    "user": re.compile(r"^usr_[a-z0-9]{20}$"),
}


class CeilingError(ValueError):
    """A ceiling document that is not the whole org or a listed set of groups and users."""


@dataclass(frozen=True, slots=True)
class Ceiling:
    """``subjects`` None is the whole org; otherwise the listed ``(kind, id)`` subjects."""

    subjects: frozenset[Subject] | None = None

    @property
    def whole_org(self) -> bool:
        return self.subjects is None


ORG: Final = Ceiling()


def parse_ceiling(raw: Mapping[str, Any]) -> Ceiling:
    """A stored or submitted ceiling document; :class:`CeilingError` when it is anything else."""
    audience = raw.get("audience")
    if audience == "org" and set(raw) == {"audience"}:
        return ORG
    listed = raw.get("subjects")
    if audience != "subjects" or set(raw) != {"audience", "subjects"}:
        raise CeilingError("a ceiling is the whole org or a list of subjects")
    if not isinstance(listed, list) or not 1 <= len(cast(list[Any], listed)) <= MAX_SUBJECTS:
        raise CeilingError(f"a ceiling lists 1 to {MAX_SUBJECTS} subjects")
    subjects: set[Subject] = set()
    for item in cast(list[Any], listed):
        if not isinstance(item, dict) or set(cast(dict[str, Any], item)) != {"kind", "id"}:
            raise CeilingError("a subject is a kind and an id")
        kind, sid = cast(dict[str, Any], item)["kind"], cast(dict[str, Any], item)["id"]
        pattern = _ID.get(kind) if isinstance(kind, str) else None
        if pattern is None or not isinstance(sid, str) or not pattern.match(sid):
            raise CeilingError("a subject is a group or a user with its own id")
        subjects.add((kind, sid))
    return Ceiling(frozenset(subjects))


def ceiling_json(ceiling: Ceiling) -> dict[str, Any]:
    """The stored and returned document, subjects in a stable order."""
    if ceiling.subjects is None:
        return {"audience": "org"}
    return {
        "audience": "subjects",
        "subjects": [{"kind": k, "id": i} for k, i in sorted(ceiling.subjects)],
    }


def ceiling_view(ceiling: Ceiling) -> list[str]:
    """The audit view: ``org``, or ``group:<id>`` and ``user:<id>`` entries."""
    if ceiling.subjects is None:
        return ["org"]
    return [f"{k}:{i}" for k, i in sorted(ceiling.subjects)]


def with_members(ceiling: Ceiling, members: Iterable[str]) -> Ceiling:
    """``ceiling`` with each of ``members`` (users the caller found to be active members of a
    listed group) listed too; the whole-org ceiling is returned as it is."""
    if ceiling.subjects is None:
        return ceiling
    return Ceiling(ceiling.subjects | {("user", m) for m in members})


def exceeds(ceiling: Ceiling, grants: Collection[GrantKey]) -> bool:
    """Whether the audience ``grants`` gives goes beyond ``ceiling``."""
    if ceiling.subjects is None:
        return False
    return any(kind == "org" or (kind, sid) not in ceiling.subjects for _, kind, sid in grants)


def lowers(old: Ceiling, new: Ceiling) -> bool:
    """Whether ``new`` admits less than ``old``: the org to a list, or a list that drops a
    subject."""
    if new.subjects is None:
        return False
    return old.subjects is None or not old.subjects <= new.subjects
