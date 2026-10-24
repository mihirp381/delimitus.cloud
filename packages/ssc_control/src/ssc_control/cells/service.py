"""The operator's writes to ``ssc.cell``. Each runs in one transaction as ``ssc_app``. A write to
a cell whose org exists is audited in that org's chain, with the operator as actor; any other is
logged as one line on ``ssc.cells``."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import AuditAction
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.db import all_org_ids, bind_org
from ssc_control.runtime.cells import CellConfig
from ssc_shared.hosts import PROOF_LABELS

type Stage = Literal["staging", "prod"]

log = logging.getLogger("ssc.cells")

_COLUMNS: Final = "label, stage, status, project_number, org_id, last_error, updated_at"
_SELECT = text(f"select {_COLUMNS} from ssc.cell where label = :label")  # noqa: S608
_LOCK = text(f"select {_COLUMNS}, identity_jwks from ssc.cell where label = :label for update")  # noqa: S608
_ALL = text(f"select {_COLUMNS} from ssc.cell order by label")  # noqa: S608
_BY_ORG = text("select label from ssc.cell where org_id = :org")
_ORG_EXISTS = text("select 1 from ssc.org_index where org_id = :org")
_ORG_ON = text("select 1 from ssc.org where id = :org and cell_label = :label")
_INSERT = text(
    "insert into ssc.cell (label, stage, status, identity_jwks, org_id) "
    "values (:label, :stage, :status, cast(:jwks as jsonb), :org)"
)
_SET = text(
    "update ssc.cell set status = :status, "
    "identity_jwks = coalesce(cast(:jwks as jsonb), identity_jwks), "
    "project_number = coalesce(:number, project_number), last_error = :error, "
    "updated_at = now() where label = :label"
)


class CellError(ValueError):
    """The cell cannot be changed as asked."""


@dataclass(frozen=True, slots=True)
class Cell:
    label: str
    stage: str
    status: str
    project_number: int | None
    org_id: str | None
    last_error: str | None
    updated_at: datetime

    @property
    def line(self) -> str:
        project = "-" if self.project_number is None else self.project_number
        line = (
            f"{self.label} {self.stage} {self.status} project={project} "
            f"org={self.org_id or '-'} updated={self.updated_at.isoformat()}"
        )
        return f"{line} error={self.last_error}" if self.last_error else line


async def get(engine: AsyncEngine, label: str) -> Cell:
    async with engine.connect() as conn:
        found = (await conn.execute(_SELECT, {"label": label})).first()
        await conn.rollback()
    if found is None:
        raise CellError(f"no cell {label}")
    return Cell(*found)


async def list_all(engine: AsyncEngine) -> list[Cell]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_ALL)).all()
        await conn.rollback()
    return [Cell(*r) for r in rows]


async def request(
    engine: AsyncEngine, label: str, *, stage: Stage, org_id: str | None, actor: Actor
) -> Cell:
    if label in PROOF_LABELS:
        raise CellError(f"{label} is a live proof-run cell: import it with import-env")
    async with engine.begin() as conn:
        if await _locked(conn, label) is not None:
            raise CellError(f"cell {label} already exists")
        if org_id is not None:
            taken = (await conn.execute(_BY_ORG, {"org": org_id})).scalar_one_or_none()
            if taken is not None:
                raise CellError(f"{org_id} already has cell {taken}")
        params = {"label": label, "stage": stage, "status": "building", "jwks": None}
        await conn.execute(_INSERT, {**params, "org": org_id})
        await _record(
            conn,
            AuditAction.CELL_REQUESTED,
            label,
            org_id=org_id,
            actor=actor,
            before=None,
            after={"stage": stage, "status": "building"},
        )
    return await get(engine, label)


async def ready(
    engine: AsyncEngine,
    label: str,
    *,
    jwks: Mapping[str, object],
    project_number: int,
    actor: Actor,
) -> Cell:
    async with engine.begin() as conn:
        cell, _ = await _existing(conn, label)
        if cell.status not in {"building", "failed"}:
            raise CellError(f"cell {label} is {cell.status}, not building or failed")
        await _set(conn, label, "ready", jwks=dict(jwks), number=project_number)
        await _record(
            conn,
            AuditAction.CELL_READY,
            label,
            org_id=cell.org_id,
            actor=actor,
            before={"status": cell.status},
            after={"status": "ready", "project_number": project_number},
        )
    return await get(engine, label)


async def fail(engine: AsyncEngine, label: str, *, reason: str, actor: Actor) -> Cell:
    async with engine.begin() as conn:
        cell, _ = await _existing(conn, label)
        if cell.status != "building":
            raise CellError(f"cell {label} is {cell.status}, not building")
        await _set(conn, label, "failed", error=reason)
        await _record(
            conn,
            AuditAction.CELL_FAILED,
            label,
            org_id=cell.org_id,
            actor=actor,
            before={"status": cell.status},
            after={"status": "failed", "reason": reason},
        )
    return await get(engine, label)


async def retire(engine: AsyncEngine, label: str, *, actor: Actor) -> Cell:
    orgs = await all_org_ids(engine)
    async with engine.begin() as conn:
        cell, _ = await _existing(conn, label)
        if cell.status == "retired":
            raise CellError(f"cell {label} is already retired")
        for org_id in orgs:
            await bind_org(conn, org_id)
            if (await conn.execute(_ORG_ON, {"org": org_id, "label": label})).first():
                raise CellError(f"{org_id} is on cell {label}: move the org first")
        await _set(conn, label, "retired", error=cell.last_error)
        await _record(
            conn,
            AuditAction.CELL_RETIRED,
            label,
            org_id=cell.org_id,
            actor=actor,
            before={"status": cell.status},
            after={"status": "retired"},
        )
    return await get(engine, label)


async def import_cells(
    engine: AsyncEngine, cells: Mapping[str, CellConfig], *, stage: Stage, actor: Actor
) -> tuple[list[str], list[str]]:
    """Write each cell as ``ready``: (imported, unchanged), or :class:`CellError` and nothing."""
    imported: list[str] = []
    unchanged: list[str] = []
    async with engine.begin() as conn:
        for label in sorted(cells):
            jwks = dict(cells[label].identity_jwks)
            found = await _locked(conn, label)
            if found is None:
                params = {"label": label, "stage": stage, "status": "ready", "org": None}
                await conn.execute(_INSERT, {**params, "jwks": json.dumps(jwks)})
                await _record(
                    conn,
                    AuditAction.CELL_READY,
                    label,
                    org_id=None,
                    actor=actor,
                    before=None,
                    after={"stage": stage, "status": "ready"},
                )
                imported.append(label)
                continue
            cell, current = found
            if cell.stage != stage:
                raise CellError(f"cell {label} is {cell.stage}, not {stage}")
            if cell.status != "ready":
                raise CellError(f"cell {label} is {cell.status}: make it ready with ready")
            if current != jwks:
                raise CellError(f"cell {label} is ready with other keys: refusing to change them")
            unchanged.append(label)
    return imported, unchanged


async def _locked(conn: AsyncConnection, label: str) -> tuple[Cell, object] | None:
    found = (await conn.execute(_LOCK, {"label": label})).first()
    return None if found is None else (Cell(*found[:-1]), found[-1])


async def _existing(conn: AsyncConnection, label: str) -> tuple[Cell, object]:
    found = await _locked(conn, label)
    if found is None:
        raise CellError(f"no cell {label}")
    return found


async def _set(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    label: str,
    status: str,
    *,
    jwks: dict[str, object] | None = None,
    number: int | None = None,
    error: str | None = None,
) -> None:
    doc = None if jwks is None else json.dumps(jwks)
    await conn.execute(
        _SET, {"label": label, "status": status, "jwks": doc, "number": number, "error": error}
    )


async def _record(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    action: AuditAction,
    label: str,
    *,
    org_id: str | None,
    actor: Actor,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    if org_id is not None and (await conn.execute(_ORG_EXISTS, {"org": org_id})).first():
        await bind_org(conn, org_id)
        await append_event(
            conn,
            NewEvent(
                org_id=org_id,
                action=action,
                actor=actor,
                target_kind="cell",
                target_id=label,
                before=before,
                after=after,
            ),
        )
        return
    log.info(
        "%s %s by %s",
        action.value,
        label,
        actor.id,
        extra={"cell_label": label, "action": action.value, "actor_id": actor.id, **after},
    )
