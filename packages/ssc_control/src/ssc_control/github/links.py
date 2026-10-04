"""Installations bound to an org and each app's connected repository, read and written inside
the org's ``bound_org`` transaction.

An installation belongs to the org an operator bound it to and to no other: its id is the
primary key of ``ssc.github_installation``, so binding it to a second org fails even though the
first org's row is invisible there. Binding is audited as ``github.installation_bound`` with the
operator as actor. A push is matched to links on the installation, the repository's numeric id
and the branch; the name is only what GitHub is called with.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.github.client import RepoRef

REPOSITORY_PATTERN: Final = r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$"
BRANCH_PATTERN: Final = r"^[A-Za-z0-9._/-]{1,255}$"
"""What ``ssc.repo_link`` accepts; the same as its CHECK constraints."""
_REPOSITORY: Final = re.compile(REPOSITORY_PATTERN)
_BRANCH: Final = re.compile(BRANCH_PATTERN)

_INSERT_INSTALLATION = text(
    "insert into ssc.github_installation (installation_id, org_id) values (:id, :org) "
    "on conflict do nothing returning installation_id"
)
_SELECT_INSTALLATION = text(
    "select 1 from ssc.github_installation where org_id = :org and installation_id = :id"
)
_SELECT_INSTALLATIONS = text(
    "select installation_id from ssc.github_installation where org_id = :org "
    "order by installation_id"
)
_LINK_COLUMNS = (
    "select app_id, installation_id, repository_id, repository, branch, required_checks, "
    "updated_at from ssc.repo_link "
)
_SELECT_LINK = text(_LINK_COLUMNS + "where org_id = :org and app_id = :app")
_LOCK_LINK = text(_SELECT_LINK.text + " for update")
_SELECT_PUSHED = text(
    _LINK_COLUMNS + "where org_id = :org and installation_id = :inst and repository_id = :repo "
    "and branch = :branch order by app_id"
)
_UPSERT_LINK = text(
    "insert into ssc.repo_link (org_id, app_id, installation_id, repository_id, repository, "
    "branch, required_checks) values (:org, :app, :inst, :repo_id, :repo, :branch, "
    "cast(:checks as jsonb)) on conflict (org_id, app_id) do update set "
    "installation_id = excluded.installation_id, repository_id = excluded.repository_id, "
    "repository = excluded.repository, branch = excluded.branch, "
    "required_checks = excluded.required_checks, updated_at = now() "
    "returning app_id, installation_id, repository_id, repository, branch, required_checks, "
    "updated_at"
)
_DELETE_LINK = text(
    "delete from ssc.repo_link where org_id = :org and app_id = :app "
    "returning app_id, installation_id, repository_id, repository, branch, required_checks, "
    "updated_at"
)


class BindError(RuntimeError):
    pass


def valid_repository(full_name: str) -> bool:
    return _REPOSITORY.fullmatch(full_name) is not None


def valid_branch(branch: str) -> bool:
    return _BRANCH.fullmatch(branch) is not None


def push_actor(installation_id: int) -> Actor:
    """Who the push job acts as for an installation: ``integration`` / ``github:<id>``."""
    return Actor(ActorKind.INTEGRATION, f"github:{installation_id}")


@dataclass(frozen=True, slots=True)
class RequiredCheck:
    """A check run named ``name`` from a run of the workflow file ``workflow``."""

    name: str
    workflow: str

    def label(self) -> str:
        return f"{self.workflow}:{self.name}"


@dataclass(frozen=True, slots=True)
class Link:
    app_id: str
    installation_id: int
    repository_id: int
    repository: str
    branch: str
    required_checks: tuple[RequiredCheck, ...]
    updated_at: Any = None

    @property
    def repo(self) -> RepoRef:
        return RepoRef(self.installation_id, self.repository_id, self.repository)

    def view(self) -> dict[str, object]:
        """What the audit row says about the link: ids and the branch, never the name."""
        return {
            "installation_id": self.installation_id,
            "repository_id": self.repository_id,
            "branch": self.branch,
            "required_checks": [c.label() for c in self.required_checks],
        }


def _checks(raw: object) -> tuple[RequiredCheck, ...]:
    items = cast("list[dict[str, str]]", json.loads(raw) if isinstance(raw, str) else raw or [])
    return tuple(RequiredCheck(name=i["name"], workflow=i["workflow"]) for i in items)


def _link(row: RowMapping) -> Link:
    return Link(
        app_id=str(row["app_id"]),
        installation_id=int(row["installation_id"]),
        repository_id=int(row["repository_id"]),
        repository=str(row["repository"]),
        branch=str(row["branch"]),
        required_checks=_checks(row["required_checks"]),
        updated_at=row["updated_at"],
    )


async def bind_installation(
    conn: AsyncConnection, org_id: str, installation_id: int, *, actor: Actor
) -> bool:
    """Bind ``installation_id`` to the org: True when newly bound, False when it already was;
    :class:`BindError` when another org holds it."""
    params = {"org": org_id, "id": installation_id}
    if (await conn.execute(_INSERT_INSTALLATION, params)).first() is None:
        if (await conn.execute(_SELECT_INSTALLATION, params)).first() is None:
            raise BindError(f"installation {installation_id} is bound to another org")
        return False
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.GITHUB_INSTALLATION_BOUND,
            actor=actor,
            target_kind="github_installation",
            target_id=str(installation_id),
            after={"installation_id": installation_id},
        ),
    )
    return True


async def installations(conn: AsyncConnection, org_id: str) -> list[int]:
    rows = await conn.execute(_SELECT_INSTALLATIONS, {"org": org_id})
    return [int(i) for i in rows.scalars()]


async def has_installation(conn: AsyncConnection, org_id: str, installation_id: int) -> bool:
    params = {"org": org_id, "id": installation_id}
    return (await conn.execute(_SELECT_INSTALLATION, params)).first() is not None


async def link_of(
    conn: AsyncConnection, org_id: str, app_id: str, *, lock: bool = False
) -> Link | None:
    sql = _LOCK_LINK if lock else _SELECT_LINK
    row = (await conn.execute(sql, {"org": org_id, "app": app_id})).mappings().first()
    return None if row is None else _link(row)


async def links_for_push(
    conn: AsyncConnection, org_id: str, *, installation_id: int, repository_id: int, branch: str
) -> list[Link]:
    params = {"org": org_id, "inst": installation_id, "repo": repository_id, "branch": branch}
    return [_link(r) for r in (await conn.execute(_SELECT_PUSHED, params)).mappings()]


async def upsert_link(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    app_id: str,
    *,
    installation_id: int,
    repository_id: int,
    repository: str,
    branch: str,
    required_checks: Sequence[RequiredCheck],
) -> Link:
    checks = [{"name": c.name, "workflow": c.workflow} for c in required_checks]
    row = (
        (
            await conn.execute(
                _UPSERT_LINK,
                {
                    "org": org_id,
                    "app": app_id,
                    "inst": installation_id,
                    "repo_id": repository_id,
                    "repo": repository,
                    "branch": branch,
                    "checks": json.dumps(checks),
                },
            )
        )
        .mappings()
        .one()
    )
    return _link(row)


async def delete_link(conn: AsyncConnection, org_id: str, app_id: str) -> Link | None:
    row = (await conn.execute(_DELETE_LINK, {"org": org_id, "app": app_id})).mappings().first()
    return None if row is None else _link(row)
