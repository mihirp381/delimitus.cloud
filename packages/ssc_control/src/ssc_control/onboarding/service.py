"""Onboarding one customer: the cell track and the identity track of an ``ssc.onboarding`` row,
moved by ``tick`` and the operator's commands. Every write locks the row and writes nothing when
nothing changed. Log lines name the row id and its states, never an address."""

import logging
import re
import secrets
import string
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.cells import service as cells
from ssc_control.db import bind_org, check_org_id
from ssc_control.db.orgs import SYSTEM_ACTOR
from ssc_control.identity import connections
from ssc_control.identity.connections import ConnectError
from ssc_control.identity.operator import (
    check_operator_id,
    check_reason,
    create_org_with_directory,
)
from ssc_control.identity.rules import (
    DirectoryPerson,
    JoinRule,
    ProfileError,
    connection_type_refused,
    join_rule_for,
)
from ssc_control.identity.workos import AmbiguousEmailError, WorkOSClient, WorkOSError
from ssc_control.notifications.service import queue
from ssc_shared.hosts import PROOF_LABELS, check_cell_label

type Stage = Literal["staging", "prod"]
type Reason = Literal[
    "JOIN_RULE_CONFIRM", "IDP_UNSUPPORTED", "ADMIN_NOT_IN_DIRECTORY", "WORKOS_ERROR"
]

log = logging.getLogger("ssc.onboarding")

USABLE_CONNECTION: Final = "active"
USABLE_DIRECTORY: Final = "linked"
MAX_ERROR: Final = 500
_LABEL_TAIL: Final = string.ascii_lowercase + string.digits
_DOMAIN: Final = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
_EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+")
_ADDRESS: Final = re.compile(r"\S+@\S+")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_WORKOS_FAILURES: Final = (WorkOSError, ProfileError, httpx2.HTTPError)

_COLUMNS: Final = (
    "id, org_id, label, stage, customer_name, domain, admin_email, workos_organization_id, "
    "sso_link, dsync_link, join_rule, status, cell_state, idp_state, reason, operator, "
    "last_error, created_at, updated_at"
)
_FIND = text(f"select {_COLUMNS} from ssc.onboarding where id = :key or label = :key")  # noqa: S608
_LOCK = text(f"select {_COLUMNS} from ssc.onboarding where id = :id for update")  # noqa: S608
_ALL = text(f"select {_COLUMNS} from ssc.onboarding order by created_at, id")  # noqa: S608
_OPEN = text("select id from ssc.onboarding where status = 'open' order by created_at, id")
_INSERT = text(
    "insert into ssc.onboarding (id, org_id, label, stage, customer_name, domain, admin_email, "
    "operator, created_at, updated_at) values (:id, :org_id, :label, :stage, :customer_name, "
    ":domain, :admin_email, :operator, :now, :now)"
)
_UPDATE = text(
    "update ssc.onboarding set workos_organization_id = :workos_organization_id, "
    "sso_link = :sso_link, dsync_link = :dsync_link, join_rule = :join_rule, status = :status, "
    "cell_state = :cell_state, idp_state = :idp_state, reason = :reason, "
    "last_error = :last_error, updated_at = :now where id = :id"
)
_LINKS = text(
    "update ssc.onboarding set sso_link = :sso, dsync_link = :dsync, updated_at = :now "
    "where id = :id"
)
_LABEL_TAKEN = text("select 1 from ssc.cell where label = :label")
_ORG_TAKEN = text(
    "select 1 from ssc.org_index where org_id = :org "
    "union all select 1 from ssc.onboarding where org_id = :org"
)
_WORKOS_TAKEN = text(
    "select 1 from ssc.onboarding where workos_organization_id = :wo "
    "union all select 1 where ssc.org_for_workos_organization(:wo) is not null"
)
_ORG_EXISTS = text("select 1 from ssc.org_index where org_id = :org")
_ADMIN = text(
    "select id from ssc.user_account where org_id = :org and role = 'admin' and status = 'active' "
    "order by created_at, id limit 1"
)


class OnboardingError(ValueError):
    """The onboarding cannot be changed as asked."""


@dataclass(frozen=True, slots=True)
class Onboarding:
    id: str
    org_id: str
    label: str
    stage: str
    customer_name: str
    domain: str
    admin_email: str
    workos_organization_id: str | None
    sso_link: str | None
    dsync_link: str | None
    join_rule: str | None
    status: str
    cell_state: str
    idp_state: str
    reason: str | None
    operator: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def line(self) -> str:
        line = (
            f"{self.id} {self.label} {self.stage} org={self.org_id} {self.status} "
            f"cell={self.cell_state} identity={self.idp_state} "
            f"updated={self.updated_at.isoformat()}"
        )
        if self.reason:
            line += f" reason={self.reason}"
        return f"{line} error={self.last_error}" if self.last_error else line


@dataclass(frozen=True, slots=True)
class TickReport:
    checked: int
    changed: tuple[str, ...]
    live: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Abandoned:
    onboarding: Onboarding
    lines: tuple[str, ...]


_MUTABLE: Final = (
    "workos_organization_id",
    "sso_link",
    "dsync_link",
    "join_rule",
    "status",
    "cell_state",
    "idp_state",
    "reason",
    "last_error",
)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _error(message: str) -> str:
    first = _ADDRESS.sub("<address>", message.strip().splitlines()[0] if message.strip() else "")
    return (first or "error")[:MAX_ERROR]


def _check_text(value: str, what: str, most: int) -> str:
    value = value.strip()
    if not 1 <= len(value) <= most or _CONTROL.search(value):
        raise OnboardingError(f"{what} is 1 to {most} characters on one line")
    return value


def _check_domain(value: str) -> str:
    domain = value.strip().lower()
    if len(domain) > 253 or _DOMAIN.fullmatch(domain) is None:
        raise OnboardingError("the domain is a host name such as example.com")
    return domain


def _check_email(value: str) -> str:
    email = value.strip().lower()
    if not 3 <= len(email) <= 320 or _EMAIL.fullmatch(email) is None:
        raise OnboardingError("the admin email is one address")
    return email


def _label() -> str:
    head = secrets.choice(string.ascii_lowercase)
    return head + "".join(secrets.choice(_LABEL_TAIL) for _ in range(11))


def _logged(ob: Onboarding, event: str, **extra: Any) -> None:
    log.info(
        "onboarding %s %s: %s cell=%s identity=%s reason=%s",
        ob.id,
        event,
        ob.status,
        ob.cell_state,
        ob.idp_state,
        ob.reason or "-",
        extra={
            "onboarding_id": ob.id,
            "event": event,
            "status": ob.status,
            "cell_state": ob.cell_state,
            "idp_state": ob.idp_state,
            "reason": ob.reason,
            **extra,
        },
    )


async def _locked(conn: AsyncConnection, onboarding_id: str) -> Onboarding:
    found = (await conn.execute(_LOCK, {"id": onboarding_id})).first()
    if found is None:
        raise OnboardingError(f"no onboarding {onboarding_id}")
    return Onboarding(*found)


async def _find(conn: AsyncConnection, key: str) -> Onboarding:
    found = (await conn.execute(_FIND, {"key": key})).first()
    if found is None:
        raise OnboardingError(f"no onboarding {key}")
    return await _locked(conn, found[0])


async def _write(conn: AsyncConnection, old: Onboarding, new: Onboarding, now: datetime) -> bool:
    if all(getattr(old, f) == getattr(new, f) for f in _MUTABLE):
        return False
    await conn.execute(
        _UPDATE, {**{f: getattr(new, f) for f in _MUTABLE}, "id": new.id, "now": now}
    )
    return True


async def status(engine: AsyncEngine, id_or_label: str) -> Onboarding:
    async with engine.connect() as conn:
        found = (await conn.execute(_FIND, {"key": id_or_label})).first()
        await conn.rollback()
    if found is None:
        raise OnboardingError(f"no onboarding {id_or_label}")
    return Onboarding(*found)


async def list_all(engine: AsyncEngine) -> list[Onboarding]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_ALL)).all()
        await conn.rollback()
    return [Onboarding(*r) for r in rows]


async def start(  # noqa: PLR0913  (keyword-only)
    engine: AsyncEngine,
    workos: WorkOSClient,
    *,
    customer_name: str,
    domain: str,
    admin_email: str,
    stage: Stage,
    operator: str,
    label: str | None = None,
    org_id: str | None = None,
    now: datetime | None = None,
) -> Onboarding:
    if stage not in ("staging", "prod"):
        raise OnboardingError("the stage is staging or prod")
    params = {
        "id": new_id("onb"),
        "customer_name": _check_text(customer_name, "the customer name", 120),
        "domain": _check_domain(domain),
        "admin_email": _check_email(admin_email),
        "stage": stage,
        "operator": _check_operator(operator),
        "now": _now(now),
    }
    org = check_org_id(org_id) if org_id is not None else new_id("org")
    async with engine.begin() as conn:
        if (await conn.execute(_ORG_TAKEN, {"org": org})).first():
            raise OnboardingError(f"{org} is already onboarded or exists")
        chosen = check_cell_label(label) if label is not None else await _free_label(conn)
        try:
            await cells.request_in(
                conn,
                chosen,
                stage=stage,
                org_id=org,
                actor=Actor(kind=ActorKind.OPERATOR, id=params["operator"]),
            )
        except cells.CellError as e:
            raise OnboardingError(str(e)) from e
        await conn.execute(_INSERT, {**params, "org_id": org, "label": chosen})
        ob = await _locked(conn, str(params["id"]))
        _logged(ob, "started", cell_label=chosen, operator=ob.operator)
    async with engine.begin() as conn:
        ob = await _locked(conn, ob.id)
        new = await _identity_requested(conn, workos, ob)
        await _write(conn, ob, new, _now(now))
        if new != ob:
            _logged(new, "workos")
    return await status(engine, ob.id)


def _check_operator(value: str) -> str:
    try:
        return check_operator_id(value)
    except ValueError as e:
        raise OnboardingError(str(e)) from e


async def _free_label(conn: AsyncConnection) -> str:
    while True:
        label = _label()
        if (
            label not in PROOF_LABELS
            and not (await conn.execute(_LABEL_TAKEN, {"label": label})).first()
        ):
            return label


async def _identity_requested(
    conn: AsyncConnection, workos: WorkOSClient, ob: Onboarding
) -> Onboarding:
    """The WorkOS organisation (made once, else reused) and both links: ``awaiting_idp``, or
    ``WORKOS_ERROR`` keeping whatever was made."""
    new = ob
    try:
        if new.workos_organization_id is None:
            new = replace(new, workos_organization_id=await _organisation(conn, workos, ob))
        assert new.workos_organization_id is not None
        sso = await workos.portal_link(organization=new.workos_organization_id, intent="sso")
        dsync = await workos.portal_link(organization=new.workos_organization_id, intent="dsync")
    except (OnboardingError, *_WORKOS_FAILURES) as e:
        return replace(new, reason="WORKOS_ERROR", last_error=_error(str(e)))
    return replace(
        new, sso_link=sso, dsync_link=dsync, idp_state="awaiting_idp", reason=None, last_error=None
    )


async def _organisation(conn: AsyncConnection, workos: WorkOSClient, ob: Onboarding) -> str:
    found = await workos.organizations_for_domain(ob.domain)
    if len(found) > 1:
        raise OnboardingError(f"{len(found)} WorkOS organisations have verified the domain")
    if found:
        (wo,) = found
        if (await conn.execute(_WORKOS_TAKEN, {"wo": wo})).first():
            raise OnboardingError(f"WorkOS organisation {wo} has the domain and is already used")
        return wo
    return await workos.create_organization(name=ob.customer_name, domain=ob.domain)


async def refresh_links(
    engine: AsyncEngine,
    workos: WorkOSClient,
    *,
    id_or_label: str,
    now: datetime | None = None,
) -> Onboarding:
    """New Admin Portal links: WorkOS links expire five minutes after they are made."""
    async with engine.begin() as conn:
        ob = await _find(conn, id_or_label)
        if ob.status != "open" or ob.idp_state != "awaiting_idp":
            raise OnboardingError(f"{ob.id} is {ob.status} {ob.idp_state}: no links to send")
        assert ob.workos_organization_id is not None
        try:
            sso = await workos.portal_link(organization=ob.workos_organization_id, intent="sso")
            dsync = await workos.portal_link(organization=ob.workos_organization_id, intent="dsync")
        except _WORKOS_FAILURES as e:
            raise OnboardingError(_error(str(e))) from e
        await conn.execute(_LINKS, {"sso": sso, "dsync": dsync, "now": _now(now), "id": ob.id})
        _logged(ob, "links")
    return await status(engine, ob.id)


async def tick(engine: AsyncEngine, workos: WorkOSClient, now: datetime) -> TickReport:
    async with engine.connect() as conn:
        ids = [r for (r,) in (await conn.execute(_OPEN)).all()]
        await conn.rollback()
    changed: list[str] = []
    live: list[str] = []
    failed: list[str] = []
    for onboarding_id in ids:
        async with engine.begin() as conn:
            ob = await _locked(conn, onboarding_id)
            if ob.status != "open":
                continue
            new = await _advance(engine, conn, workos, ob)
            if new.reason == "WORKOS_ERROR":
                failed.append(ob.id)
            if new.status == "live":
                live.append(ob.id)
            if await _write(conn, ob, new, now):
                changed.append(ob.id)
                _logged(new, "tick")
    return TickReport(len(ids), tuple(changed), tuple(live), tuple(failed))


async def _advance(
    engine: AsyncEngine, conn: AsyncConnection, workos: WorkOSClient, ob: Onboarding
) -> Onboarding:
    new = ob
    if new.cell_state == "awaiting_cell" and (await cells.get(engine, ob.label)).status == "ready":
        new = replace(new, cell_state="cell_ready")
    if new.idp_state == "requested":
        new = await _identity_requested(conn, workos, new)
    if new.idp_state == "awaiting_idp":
        new = await _identity_waiting(engine, conn, workos, new)
    if new.cell_state == "cell_ready" and new.idp_state == "org_created":
        await _go_live(conn, new)
        new = replace(new, status="live")
    return new


def _waiting(ob: Onboarding, reason: Reason | None, error: str | None = None) -> Onboarding:
    return replace(ob, reason=reason, last_error=None if error is None else _error(error))


async def _identity_waiting(  # noqa: PLR0911
    engine: AsyncEngine, conn: AsyncConnection, workos: WorkOSClient, ob: Onboarding
) -> Onboarding:
    assert ob.workos_organization_id is not None
    if (await conn.execute(_ORG_EXISTS, {"org": ob.org_id})).first():
        return await _already_created(conn, ob)
    try:
        conns = await workos.connections(ob.workos_organization_id)
        dirs = await workos.directories(ob.workos_organization_id)
    except _WORKOS_FAILURES as e:
        return _waiting(ob, "WORKOS_ERROR", str(e))
    active = [c for c in conns if c.state == USABLE_CONNECTION]
    linked = [d for d in dirs if d.state == USABLE_DIRECTORY]
    if not active or not linked:
        return _waiting(ob, None)
    if len(linked) > 1:
        return _waiting(ob, "IDP_UNSUPPORTED", f"{len(linked)} linked directories: keep one")
    (directory,) = linked
    types = sorted({c.connection_type for c in active})
    decision = join_rule_for(directory.type, types)
    if decision == "unsupported":
        found = f"directory {directory.type}, connections {', '.join(types)}"
        return _waiting(ob, "IDP_UNSUPPORTED", f"{found}: not supported")
    if decision == "confirm_email" and ob.join_rule != "email":
        return _waiting(ob, "JOIN_RULE_CONFIRM")
    rule: JoinRule = "email" if decision == "confirm_email" else "idp_id"
    person = await _admin(workos, directory.id, ob.admin_email)
    if not isinstance(person, DirectoryPerson):
        return _waiting(ob, *person)
    accepted = [c.id for c in active if not connection_type_refused(c.connection_type)]
    try:
        await create_org_with_directory(
            engine,
            workos,
            name=ob.customer_name,
            founder_name=person.display_name,
            founder_email=person.email,
            founder_idp_id=person.idp_id,
            workos_organization_id=ob.workos_organization_id,
            workos_directory_id=directory.id,
            sso_connection_ids=accepted,
            join_rule=rule,
            admin_group_ref=None,
            cell_label=ob.label,
            actor=Actor(kind=ActorKind.OPERATOR, id=ob.operator),
            org_id=ob.org_id,
        )
    except ConnectError as e:
        return _waiting(ob, "ADMIN_NOT_IN_DIRECTORY", str(e))
    except _WORKOS_FAILURES as e:
        return _waiting(ob, "WORKOS_ERROR", str(e))
    return replace(ob, join_rule=rule, idp_state="org_created", reason=None, last_error=None)


async def _admin(
    workos: WorkOSClient, directory: str, email: str
) -> DirectoryPerson | tuple[Reason, str]:
    try:
        raw = await workos.directory_user_by_email(directory, email)
    except AmbiguousEmailError as e:
        return "ADMIN_NOT_IN_DIRECTORY", str(e)
    except _WORKOS_FAILURES as e:
        return "WORKOS_ERROR", str(e)
    if raw is None:
        return "ADMIN_NOT_IN_DIRECTORY", "the admin email names no directory user"
    try:
        person = DirectoryPerson.from_wire(raw)
    except ProfileError as e:
        return "ADMIN_NOT_IN_DIRECTORY", f"the directory user has no {e}"
    if not person.active or person.guest:
        return "ADMIN_NOT_IN_DIRECTORY", f"directory user {person.id} is not an active member"
    return person


async def _already_created(conn: AsyncConnection, ob: Onboarding) -> Onboarding:
    await bind_org(conn, ob.org_id)
    found = await connections.load(conn, ob.org_id)
    rule = found.join_rule if found is not None else ob.join_rule or "idp_id"
    return replace(ob, join_rule=rule, idp_state="org_created", reason=None, last_error=None)


async def _go_live(conn: AsyncConnection, ob: Onboarding) -> None:
    await bind_org(conn, ob.org_id)
    admin = (await conn.execute(_ADMIN, {"org": ob.org_id})).scalar_one_or_none()
    recipients = [] if admin is None else [str(admin)]
    queued = await queue(
        conn,
        org_id=ob.org_id,
        kind="welcome",
        approval_id=None,
        recipients=recipients,
        key=f"welcome:{ob.org_id}",
    )
    if recipients and not queued:
        return
    await append_event(
        conn,
        NewEvent(
            org_id=ob.org_id,
            action=AuditAction.ORG_ONBOARDED,
            actor=SYSTEM_ACTOR,
            target_kind="org",
            target_id=ob.org_id,
            before=None,
            after={"cell_label": ob.label},
        ),
    )


async def confirm(
    engine: AsyncEngine,
    *,
    id_or_label: str,
    join_rule: str,
    operator: str,
    now: datetime | None = None,
) -> Onboarding:
    who = _check_operator(operator)
    if join_rule != "email":
        raise OnboardingError("only email can be confirmed: idp_id needs no confirmation")
    async with engine.begin() as conn:
        ob = await _find(conn, id_or_label)
        if ob.status != "open" or ob.reason != "JOIN_RULE_CONFIRM":
            raise OnboardingError(f"{ob.id} is not waiting for a join rule")
        new = replace(ob, join_rule="email", reason=None, last_error=None)
        await _write(conn, ob, new, _now(now))
        _logged(new, "confirmed", join_rule="email", operator=who)
    return await status(engine, ob.id)


async def abandon(
    engine: AsyncEngine,
    *,
    id_or_label: str,
    operator: str,
    reason: str,
    now: datetime | None = None,
) -> Abandoned:
    who = _check_operator(operator)
    why = _check_reason(reason)
    async with engine.begin() as conn:
        ob = await _find(conn, id_or_label)
        if ob.status == "live":
            raise OnboardingError(f"{ob.id} is live: an onboarded org is not abandoned")
        if ob.status == "abandoned":
            raise OnboardingError(f"{ob.id} is already abandoned")
        try:
            await cells.retire(engine, ob.label, actor=Actor(kind=ActorKind.OPERATOR, id=who))
        except cells.CellError as e:
            if "already retired" not in str(e):
                raise OnboardingError(f"abandon refused: {e}") from e
        new = replace(ob, status="abandoned", reason=None)
        await _write(conn, ob, new, _now(now))
        _logged(new, "abandoned", operator=who, why=why)
    lines = [f"uv run python -m ssc_infra.onboard {ob.label} --stage {ob.stage} --destroy"]
    if ob.workos_organization_id is not None:
        lines.append(
            f"WorkOS organisation {ob.workos_organization_id} is kept: delete it in WorkOS if the "
            "customer is not coming back"
        )
    return Abandoned(await status(engine, ob.id), tuple(lines))


def _check_reason(value: str) -> str:
    try:
        return check_reason(value)
    except ValueError as e:
        raise OnboardingError(str(e)) from e
