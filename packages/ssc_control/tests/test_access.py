"""SSC-021: sharing rules, the directory and published access snapshots (decision 019).

Ticket "done when" checks:
  * removed from a group, a user loses access at the next snapshot
        -> test_group_removal_loses_access_in_the_next_version
  * a malformed snapshot keeps the last good view
        -> the same test, and ssc_shared's test_access_eval.py
  * an app cannot exist without an owner -> test_the_owner_cannot_be_unset_or_deleted
  * the last admin cannot be removed -> test_the_directory_cannot_remove_the_last_admin
  * explain names the grant -> test_explain_names_the_grant_and_the_group
Plus: floors, who may change sharing, one evaluator, content-addressed publishing, the
audience-ceiling hook, the compile job, heartbeat acknowledgements and the 0009 round trip.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from procrastinate import App, PsycopgConnector
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.engine import make_url
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, mint, new_key

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, authz, create_app
from ssc_control.api.auth import Principal, PrincipalKind
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.problems import Refusal
from ssc_control.api.settings import INTERNAL_AUDIENCE
from ssc_control.api.uow import UnitOfWork
from ssc_control.db import (
    MIGRATE_ROLE,
    NewOrg,
    bind_org_sync,
    bound_org,
    create_org,
    downgrade,
    make_engine,
    upgrade,
)
from ssc_control.domain import grant_rules
from ssc_control.snapshot import jobs as snapshot_jobs
from ssc_control.snapshot.compiler import latest_key, point_latest, publish
from ssc_control.snapshot.service import COMPILE_TASK, Snapshots, compile_lock, mark_dirty
from ssc_control.worker import build_app, queue_conninfo
from ssc_control.worker_ports import PORTS_KEY, Ports
from ssc_shared.access import ViewHolder, decide
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock
from ssc_shared.snapshot_feed import SnapshotFeed

REPO = Path(__file__).resolve().parents[3]
CELL = "cellabcd"
"""Never a generated label: those are consonants only."""


# ── the world ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    org: str
    admin: str
    admin_subject: str
    admin_token: str
    operator_token: str
    workload_token: str
    app: str
    prod: str
    preview: str


@pytest.fixture(scope="module")
def client(dsns: Dsns, signing_key: SigningKey) -> Any:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def user_token(key: SigningKey, org: str, user: str) -> str:
    return mint(key, org=org, sub=user, jti=f"cred_{new_key()[:16]}")


def internal_token(key: SigningKey, org: str, kind: str) -> str:
    return mint(
        key,
        org=org,
        sub="op_sync" if kind == "operator" else new_id("env"),
        kind=kind,
        audience=INTERNAL_AUDIENCE,
        jti=f"cred_{new_key()[:16]}",
    )


def new_org(dsn: str) -> tuple[str, str, str]:
    """A new org; its id, its admin's user id and the admin's identity subject."""
    subject = f"sub-{uuid.uuid4().hex}"

    async def go() -> tuple[str, str, str]:
        engine = make_engine(dsn)
        try:
            spec = NewOrg("Access", "Ada Admin", "ada@example.com", ISSUER, subject)
            created = await create_org(engine, spec)
        finally:
            await engine.dispose()
        return created.org_id, created.admin_user_id, subject

    return asyncio.run(go())


def create_app_as(client: TestClient, token: str, slug: str) -> dict[str, Any]:
    r = client.post(
        "/v1/apps", json={"slug": slug}, headers=auth(token, **{IDEMPOTENCY_HEADER: new_key()})
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def world(client: TestClient, dsns: Dsns, signing_key: SigningKey) -> World:
    org, admin, subject = new_org(dsns.app)
    token = user_token(signing_key, org, admin)
    app = create_app_as(client, token, "ledger")
    envs = {e["name"]: e["id"] for e in app["environments"]}
    return World(
        org=org,
        admin=admin,
        admin_subject=subject,
        admin_token=token,
        operator_token=internal_token(signing_key, org, "operator"),
        workload_token=internal_token(signing_key, org, "workload"),
        app=app["id"],
        prod=envs["prod"],
        preview=envs["preview"],
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def sync_user(  # noqa: PLR0913
    client: TestClient,
    w: World,
    subject: str,
    *,
    role: str = "member",
    status: str = "active",
    name: str = "Una User",
    email: str = "una@example.com",
    token: str | None = None,
) -> Response:
    body = {
        "issuer": ISSUER,
        "subject": subject,
        "display_name": name,
        "email": email,
        "role": role,
        "status": status,
    }
    return client.post(
        "/internal/v1/directory/users",
        json=body,
        headers=auth(token or w.operator_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def new_member(client: TestClient, w: World) -> str:
    r = sync_user(client, w, f"sub-{uuid.uuid4().hex}")
    assert r.status_code == 200, r.text
    return r.json()["user_id"]


def sync_group(client: TestClient, w: World, ref: str, name: str) -> Response:
    return client.post(
        "/internal/v1/directory/groups",
        json={"directory_ref": ref, "display_name": name},
        headers=auth(w.operator_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def set_members(client: TestClient, w: World, group: str, users: list[str]) -> Response:
    return client.put(
        f"/internal/v1/directory/groups/{group}/members",
        json={"user_ids": users},
        headers=auth(w.operator_token),
    )


def put_grants(  # noqa: PLR0913
    client: TestClient,
    w: World,
    env: str,
    token: str,
    grants: list[dict[str, Any]],
    version: int,
) -> Response:
    return client.put(
        f"/v1/apps/{w.app}/environments/{env}/grants",
        json={"grants": grants},
        headers=auth(token, **{"If-Match": f'"{version}"'}),
    )


def grant(role: str, kind: str, subject: str | None = None) -> dict[str, Any]:
    return {"role": role, "subject_kind": kind, "subject_id": subject}


def explain(
    client: TestClient, w: World, env: str, token: str, user: str | None = None
) -> Response:
    params = {} if user is None else {"user_id": user}
    return client.get(
        f"/v1/apps/{w.app}/environments/{env}/access", params=params, headers=auth(token)
    )


def sql(dsn: str, org: str, statement: str, params: tuple[object, ...] = ()) -> list[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        cur = conn.execute(statement, params)
        return cur.fetchall() if cur.description else []


def events(dsn: str, org: str, action: AuditAction) -> list[dict[str, Any]]:
    return sql(
        dsn,
        org,
        "select actor_kind, target_kind, target_id, before, after from ssc.audit_event "
        "where action = %s order by seq",
        (action.value,),
    )


def blob_store(root: Path) -> FsBlobStore:
    clock = SystemClock()
    signer = UrlSigner({"k1": secrets.token_bytes(32)}, active="k1", clock=clock)
    return FsBlobStore(root, signer=signer, base_url="http://blobs.test/v1/blobs/", clock=clock)


async def read_blob(blob: FsBlobStore, key: str) -> bytes:
    return b"".join([chunk async for chunk in blob.get(key)])


async def publish_now(dsn: str, org: str, blob: FsBlobStore) -> tuple[int, bytes]:
    """Publish the next version and move ``latest.json``; the version and its document."""
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, org) as conn:
            version = await publish(conn, org, blob, at=datetime.now(UTC))
        await point_latest(engine, org, blob)
    finally:
        await engine.dispose()
    pointer = json.loads(await read_blob(blob, latest_key(org)))
    assert pointer["version"] == version
    return version, await read_blob(blob, pointer["key"])


# ── done when: removed from a group, a user loses access ─────────────────────


def test_group_removal_loses_access_in_the_next_version(
    client: TestClient, world: World, dsns: Dsns, tmp_path: Path
) -> None:
    una = new_member(client, world)
    group = sync_group(client, world, "okta-00g1finance", "Finance")
    assert group.status_code == 200, group.text
    assert group.json()["created"] is True
    gid = group.json()["group_id"]
    added = set_members(client, world, gid, [una])
    assert added.json() == {"group_id": gid, "added": [una], "removed": []}
    r = put_grants(client, world, world.prod, world.admin_token, [grant("user", "group", gid)], 1)
    assert r.status_code == 200, r.text
    (granted,) = r.json()["grants"]

    blob = blob_store(tmp_path)
    holder = ViewHolder(world.org)
    v1, doc1 = asyncio.run(publish_now(dsns.app, world.org, blob))
    assert holder.apply(doc1) is True
    assert json.loads(doc1)["hosts"] == {"ledger": world.prod, "ledger--preview": world.preview}
    first = decide(holder.view, world.prod, una)
    assert (first.allowed, first.reason, first.role) == (True, "granted", "user")
    assert [g.grant_id for g in first.via] == [granted["id"]]

    removed = set_members(client, world, gid, [])
    assert removed.json() == {"group_id": gid, "added": [], "removed": [una]}
    (synced, emptied) = events(dsns.app, world.org, AuditAction.GROUP_SYNCED)[1:]
    assert synced["after"] == {"directory_ref": "okta-00g1finance", "added": [una], "removed": []}
    assert emptied["after"] == {"directory_ref": "okta-00g1finance", "added": [], "removed": [una]}

    # A malformed or truncated v2 is refused and v1 keeps deciding.
    for bad in (doc1[:-7], doc1.replace(b'"version":' + str(v1).encode(), b'"version":"x"')):
        with pytest.raises(ValueError, match="valid|Invalid|EOF|version"):
            holder.apply(bad)
        assert holder.view is not None
        assert holder.view.version == v1

    v2, doc2 = asyncio.run(publish_now(dsns.app, world.org, blob))
    assert v2 == v1 + 1
    assert holder.apply(doc2) is True
    later = decide(holder.view, world.prod, una)
    assert (later.allowed, later.reason, later.via) == (False, "no_grant", ())
    assert holder.apply(doc1) is False  # an older version never replaces a newer one


# ── done when: an app cannot exist without an owner ──────────────────────────


def test_the_owner_cannot_be_unset_or_deleted(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    owner = new_member(client, world)
    app = create_app_as(client, user_token(signing_key, world.org, owner), "owned")
    assert app["owner_user_id"] == owner
    with pytest.raises(psycopg.errors.NotNullViolation):
        sql(
            dsns.app,
            world.org,
            "update ssc.app set owner_user_id = null where id = %s",
            (app["id"],),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sql(dsns.app, world.org, "delete from ssc.user_account where id = %s", (owner,))


def test_the_owner_needs_no_grant_to_change_sharing(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    owner = new_member(client, world)
    token = user_token(signing_key, world.org, owner)
    app = create_app_as(client, token, "mine")
    prod = next(e["id"] for e in app["environments"] if e["name"] == "prod")
    w = World(**{**world.__dict__, "app": app["id"], "prod": prod})
    # No owner grant is created, and removing every grant is allowed: the owner keeps the
    # right to change sharing but, like everyone, needs a grant to reach the app.
    first = put_grants(client, w, prod, token, [grant("builder", "user", owner)], 1)
    assert first.status_code == 200, first.text
    emptied = put_grants(client, w, prod, token, [], 2)
    assert emptied.status_code == 200, emptied.text
    again = put_grants(client, w, prod, token, [grant("user", "org")], 3)
    assert again.status_code == 200, again.text
    assert explain(client, w, prod, token).json()["reason"] == "granted"
    assert put_grants(client, w, prod, token, [], 4).status_code == 200
    assert explain(client, w, prod, token).json()["reason"] == "no_grant"


# ── done when: the last admin cannot be demoted ──────────────────────────────


def test_the_directory_cannot_demote_the_last_admin(
    client: TestClient, world: World, dsns: Dsns
) -> None:
    r = sync_user(client, world, world.admin_subject, role="member", status="active")
    assert_problem(r, ErrorCode.LAST_ORG_ADMIN)
    (row,) = sql(
        dsns.app,
        world.org,
        "select role, status from ssc.user_account where id = %s",
        (world.admin,),
    )
    assert row == {"role": "admin", "status": "active"}
    # With a second admin, the first may step down.
    second = sync_user(client, world, "sub-second-admin", role="admin")
    assert second.status_code == 200, second.text
    stepped = sync_user(client, world, world.admin_subject, role="member", name="Ada Admin")
    assert stepped.status_code == 200, stepped.text
    assert stepped.json() == {"user_id": world.admin, "created": False}


def test_the_directory_may_deactivate_the_last_admin(
    client: TestClient, world: World, dsns: Dsns
) -> None:
    # SSC-019: directory sync is never blocked; an SSC operator restores an admin.
    r = sync_user(client, world, world.admin_subject, role="admin", status="deactivated")
    assert r.status_code == 200, r.text
    (row,) = sql(
        dsns.app,
        world.org,
        "select role, status from ssc.user_account where id = %s",
        (world.admin,),
    )
    assert row == {"role": "admin", "status": "deactivated"}


# ── the directory ────────────────────────────────────────────────────────────


def test_directory_users_are_keyed_by_identity_and_audited(
    client: TestClient, world: World, dsns: Dsns
) -> None:
    created = sync_user(client, world, "sub-una")
    assert created.status_code == 200, created.text
    uid = created.json()["user_id"]
    assert created.json()["created"] is True
    assert sync_user(client, world, "sub-una").json() == {"user_id": uid, "created": False}
    renamed = sync_user(client, world, "sub-una", name="Una Renamed")
    assert renamed.json() == {"user_id": uid, "created": False}
    assert sync_user(client, world, "sub-una", name="Una Renamed", role="admin").status_code == 200
    gone = sync_user(
        client, world, "sub-una", name="Una Renamed", role="admin", status="deactivated"
    )
    assert gone.status_code == 200
    back = sync_user(client, world, "sub-una", name="Una Renamed", role="admin")
    assert back.status_code == 200
    mine = lambda action: [e for e in events(dsns.app, world.org, action) if e["target_id"] == uid]  # noqa: E731
    (made,) = mine(AuditAction.USER_CREATED)
    assert (made["actor_kind"], made["after"]) == (
        "operator",
        {"role": "member", "status": "active"},
    )
    assert [(e["before"], e["after"]) for e in mine(AuditAction.USER_UPDATED)] == [
        (None, None),
        ({"role": "member"}, {"role": "admin"}),
    ]
    assert [e["after"] for e in mine(AuditAction.USER_DEACTIVATED)] == [{"status": "deactivated"}]
    assert [e["after"] for e in mine(AuditAction.USER_REACTIVATED)] == [{"status": "active"}]
    (row,) = sql(
        dsns.app,
        world.org,
        "select display_name, role, status, deactivated_at from ssc.user_account where id = %s",
        (uid,),
    )
    assert row == {
        "display_name": "Una Renamed",
        "role": "admin",
        "status": "active",
        "deactivated_at": None,
    }
    # A user who arrives deactivated is stored that way.
    late = sync_user(client, world, "sub-late", status="deactivated")
    assert late.status_code == 200, late.text
    (row,) = sql(
        dsns.app,
        world.org,
        "select status, deactivated_at from ssc.user_account where id = %s",
        (late.json()["user_id"],),
    )
    assert row["status"] == "deactivated"
    assert row["deactivated_at"] is not None


def test_directory_groups_and_members(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    made = sync_group(client, world, "ref-eng", "Engineering")
    gid = made.json()["group_id"]
    renamed = sync_group(client, world, "ref-eng", "Engineers")
    assert renamed.json() == {"group_id": gid, "created": False}
    (row,) = sql(
        dsns.app, world.org, "select display_name from ssc.user_group where id = %s", (gid,)
    )
    assert row["display_name"] == "Engineers"
    assert len(events(dsns.app, world.org, AuditAction.GROUP_SYNCED)) == 1

    assert_problem(set_members(client, world, new_id("grp"), []), ErrorCode.NOT_FOUND)
    assert_problem(set_members(client, world, gid, [new_id("usr")]), ErrorCode.REFERENCE_NOT_FOUND)
    other_org, other_admin, _ = new_org(dsns.app)
    assert other_org != world.org
    assert_problem(set_members(client, world, gid, [other_admin]), ErrorCode.REFERENCE_NOT_FOUND)
    a, b = new_member(client, world), new_member(client, world)
    assert set_members(client, world, gid, [a, b, a]).json()["added"] == sorted([a, b])
    swapped = set_members(client, world, gid, [b, world.admin])
    assert swapped.json() == {"group_id": gid, "added": [world.admin], "removed": [a]}
    unchanged = len(events(dsns.app, world.org, AuditAction.GROUP_SYNCED))
    assert set_members(client, world, gid, [world.admin, b]).json()["added"] == []
    assert len(events(dsns.app, world.org, AuditAction.GROUP_SYNCED)) == unchanged


def test_only_an_operator_may_sync_the_directory(
    client: TestClient, world: World, signing_key: SigningKey
) -> None:
    r = sync_user(client, world, "sub-x", token=world.workload_token)
    assert_problem(r, ErrorCode.FORBIDDEN)
    user = client.post(
        "/internal/v1/directory/groups",
        json={"directory_ref": "r", "display_name": "n"},
        headers=auth(world.admin_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert user.status_code in (401, 403)


# ── floors and who may change sharing ────────────────────────────────────────


def test_floors_and_who_may_change_sharing(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    una = new_member(client, world)
    refused = put_grants(
        client, world, world.preview, world.admin_token, [grant("user", "user", una)], 1
    )
    assert_problem(refused, ErrorCode.VALIDATION_FAILED)
    ok = put_grants(
        client, world, world.preview, world.admin_token, [grant("builder", "user", una)], 1
    )
    assert ok.status_code == 200, ok.text
    prod = put_grants(client, world, world.prod, world.admin_token, [grant("user", "user", una)], 1)
    assert prod.status_code == 200, prod.text
    dup = [grant("user", "user", una), grant("builder", "user", una)]
    assert_problem(
        put_grants(client, world, world.prod, world.admin_token, dup, 2),
        ErrorCode.VALIDATION_FAILED,
    )

    # A member with no builder grant may not change sharing; a builder on preview may change
    # preview but not prod: the check is per environment.
    outsider = user_token(signing_key, world.org, new_member(client, world))
    assert_problem(put_grants(client, world, world.prod, outsider, [], 2), ErrorCode.FORBIDDEN)
    builder = user_token(signing_key, world.org, una)
    assert_problem(put_grants(client, world, world.prod, builder, [], 2), ErrorCode.FORBIDDEN)
    kept = [grant("builder", "user", una), grant("builder", "org")]
    assert put_grants(client, world, world.preview, builder, kept, 2).status_code == 200


def test_the_audience_ceiling_hook_is_called(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[grant_rules.SharingTarget, set[Any]]] = []
    original = grant_rules.audience_ceiling

    def spy(target: grant_rules.SharingTarget, desired: set[Any]) -> None:
        seen.append((target, set(desired)))
        assert original(target, desired) is None

    monkeypatch.setattr(grant_rules, "audience_ceiling", spy)
    r = put_grants(client, world, world.prod, world.admin_token, [grant("user", "org")], 1)
    assert r.status_code == 200, r.text
    ((target, desired),) = seen
    assert (target.environment_id, target.name) == (world.prod, "prod")
    assert desired == {("user", "org", None)}


# ── explain ──────────────────────────────────────────────────────────────────


def test_explain_names_the_grant_and_the_group(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    una, vic = new_member(client, world), new_member(client, world)
    gid = sync_group(client, world, "ref-fin", "Finance").json()["group_id"]
    set_members(client, world, gid, [una])
    r = put_grants(client, world, world.prod, world.admin_token, [grant("user", "group", gid)], 1)
    (g,) = r.json()["grants"]

    ok = explain(client, world, world.prod, world.admin_token, una)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {
        "user_id": una,
        "environment_id": world.prod,
        "allowed": True,
        "role": "user",
        "floor": "user",
        "reason": "granted",
        "grants": [
            {
                "grant_id": g["id"],
                "role": "user",
                "subject_kind": "group",
                "subject_id": gid,
                "group_name": "Finance",
            }
        ],
        "evaluated_from": "live",
        "published_version": None,
    }
    none = explain(client, world, world.prod, world.admin_token, vic).json()
    assert (none["allowed"], none["reason"], none["grants"], none["role"]) == (
        False,
        "no_grant",
        [],
        None,
    )
    # A grant stored below the floor (written before floors existed) counts for nothing.
    legacy = new_id("gnt")
    sql(
        dsns.app,
        world.org,
        "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, "
        "granted_by_user_id) values (%s, %s, %s, 'user', 'user', %s, %s)",
        (legacy, world.org, world.preview, vic, world.admin),
    )
    below = explain(client, world, world.preview, world.admin_token, vic).json()
    assert (below["allowed"], below["reason"], below["floor"]) == (False, "below_floor", "builder")
    assert [x["grant_id"] for x in below["grants"]] == [legacy]
    # The caller by default; builders only; users of this org only.
    assert explain(client, world, world.prod, world.admin_token).json()["user_id"] == world.admin
    outsider = user_token(signing_key, world.org, vic)
    assert_problem(explain(client, world, world.prod, outsider, una), ErrorCode.FORBIDDEN)
    assert_problem(
        explain(client, world, world.prod, world.admin_token, new_id("usr")),
        ErrorCode.REFERENCE_NOT_FOUND,
    )
    assert_problem(
        explain(client, world, new_id("env"), world.admin_token, una), ErrorCode.NOT_FOUND
    )


def test_explain_reports_the_published_version(
    client: TestClient, world: World, dsns: Dsns, tmp_path: Path
) -> None:
    version, _ = asyncio.run(publish_now(dsns.app, world.org, blob_store(tmp_path)))
    body = explain(client, world, world.prod, world.admin_token).json()
    assert body["published_version"] == version


# ── who the caller is, finding people, the apps a builder may ship ──────────


def test_whoami_reports_the_active_org_role(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    def role(token: str) -> object:
        r = client.get("/v1/whoami", headers=auth(token))
        assert r.status_code == 200, r.text
        return r.json()["role"]

    assert role(world.admin_token) == "admin"
    subject = f"sub-{uuid.uuid4().hex}"
    una = sync_user(client, world, subject).json()["user_id"]
    token = user_token(signing_key, world.org, una)
    assert role(token) == "member"
    # Read from the directory on every call, not carried in the token.
    assert sync_user(client, world, subject, role="admin").status_code == 200
    assert role(token) == "admin"
    assert sync_user(client, world, subject, role="admin", status="deactivated").status_code == 200
    assert role(token) is None
    # Unknown here, another org's admin, or not a user credential: no role.
    _, other_admin, _ = new_org(dsns.app)
    assert role(user_token(signing_key, world.org, new_id("usr"))) is None
    assert role(user_token(signing_key, world.org, other_admin)) is None
    for kind in ("workload", "operator"):
        assert role(mint(signing_key, org=world.org, sub=world.admin, kind=kind)) is None


def test_admins_find_people_by_email(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    def find(token: str, email: str | None) -> Response:
        params = {} if email is None else {"email": email}
        return client.get("/v1/users", params=params, headers=auth(token))

    def sync(w: World, **kw: str) -> str:
        r = sync_user(client, w, f"sub-{uuid.uuid4().hex}", **kw)
        assert r.status_code == 200, r.text
        return r.json()["user_id"]

    bea = sync(world, name="Bea Two", email="Shared@Example.com")
    al = sync(world, name="Al One", email="shared@example.com", status="deactivated")
    sync(world, email="other@example.com")
    other_org, _, _ = new_org(dsns.app)
    elsewhere = World(
        **{
            **world.__dict__,
            "org": other_org,
            "operator_token": internal_token(signing_key, other_org, "operator"),
        }
    )
    sync(elsewhere, email="shared@example.com")

    # Email is not a key: every match comes back, deactivated people too, never another org's.
    found = find(world.admin_token, "SHARED@example.COM")
    assert found.status_code == 200, found.text
    assert found.json() == {
        "users": [
            {
                "id": al,
                "display_name": "Al One",
                "email": "shared@example.com",
                "role": "member",
                "status": "deactivated",
            },
            {
                "id": bea,
                "display_name": "Bea Two",
                "email": "Shared@Example.com",
                "role": "member",
                "status": "active",
            },
        ]
    }
    ada = find(world.admin_token, "ada@example.com").json()["users"]
    assert [(u["id"], u["role"]) for u in ada] == [(world.admin, "admin")]
    # The whole address or nothing: no prefix or pattern search.
    for miss in ("nobody@example.com", "shared@example", "%@example.com", "_hared@example.com"):
        assert find(world.admin_token, miss).json() == {"users": []}
    for bad in (None, "", "no-at-sign", "a@b@c", "sp ace@example.com", "x" * 320 + "@e.c"):
        assert_problem(find(world.admin_token, bad), ErrorCode.VALIDATION_FAILED)

    # Only an active admin with a user credential; an admin's agent session counts.
    agent = mint(signing_key, org=world.org, sub=world.admin, agent=True, client_id="cli_x")
    assert [u["id"] for u in find(agent, "ada@example.com").json()["users"]] == [world.admin]
    gone = f"sub-{uuid.uuid4().hex}"
    former = sync_user(client, world, gone, role="admin").json()["user_id"]
    assert sync_user(client, world, gone, role="admin", status="deactivated").status_code == 200
    refused = [
        user_token(signing_key, world.org, bea),
        user_token(signing_key, world.org, former),
        mint(signing_key, org=world.org, sub=world.admin, kind="workload"),
        mint(signing_key, org=world.org, sub=world.admin, kind="operator"),
    ]
    for token in refused:
        assert_problem(find(token, "shared@example.com"), ErrorCode.FORBIDDEN)


type Check = Callable[[UnitOfWork, str], Awaitable[str]]


async def passes(dsn: str, org: str, user: str, check: Check, ids: list[str]) -> set[str]:
    """The ids for which ``check`` (an ``authz.require_*``) lets ``user`` through, one at a time."""
    engine = make_engine(dsn)
    allowed: set[str] = set()
    try:
        async with bound_org(engine, org) as conn:
            principal = Principal(org, user, PrincipalKind.USER, "cred_rule")
            uow = UnitOfWork(conn=conn, principal=principal, request_id="req_rule")
            for one in ids:
                try:
                    await check(uow, one)
                except Refusal:
                    continue
                allowed.add(one)
    finally:
        await engine.dispose()
    return allowed


@dataclass(frozen=True)
class Cast:
    """Seven people around two apps: the world's ``ledger`` (the admin's) and ``owned``."""

    people: dict[str, str]
    owned: str
    owned_prod: str
    owned_preview: str
    owner_token: str


def cast_of_builders(client: TestClient, world: World, signing_key: SigningKey) -> Cast:
    """The admin; the owner of ``owned``; a builder on ledger's preview directly, and one through
    a group on its prod; a user of ledger's prod only; a deactivated builder; and nobody."""
    owner = new_member(client, world)
    owner_token = user_token(signing_key, world.org, owner)
    owned = create_app_as(client, owner_token, "owned")
    envs = {e["name"]: e["id"] for e in owned["environments"]}
    direct, grouped, viewer, nobody = (new_member(client, world) for _ in range(4))
    gone_subject = f"sub-{uuid.uuid4().hex}"
    gone = sync_user(client, world, gone_subject).json()["user_id"]
    gid = sync_group(client, world, "okta-00g1builders", "Builders").json()["group_id"]
    assert set_members(client, world, gid, [grouped]).status_code == 200
    on_preview = [grant("builder", "user", direct), grant("builder", "user", gone)]
    on_prod = [grant("builder", "group", gid), grant("user", "user", viewer)]
    for env, grants in ((world.preview, on_preview), (world.prod, on_prod)):
        r = put_grants(client, world, env, world.admin_token, grants, 1)
        assert r.status_code == 200, r.text
    assert sync_user(client, world, gone_subject, status="deactivated").status_code == 200
    people = {
        "admin": world.admin,
        "owner": owner,
        "direct": direct,
        "grouped": grouped,
        "viewer": viewer,
        "gone": gone,
        "nobody": nobody,
    }
    return Cast(people, owned["id"], envs["prod"], envs["preview"], owner_token)


def open_owned_to_the_org(client: TestClient, world: World, cast: Cast) -> None:
    """An org-wide builder grant on ``owned``'s preview: every active member builds it."""
    w = World(**{**world.__dict__, "app": cast.owned, "preview": cast.owned_preview})
    grants = [grant("builder", "org")]
    r = put_grants(client, w, cast.owned_preview, cast.owner_token, grants, 1)
    assert r.status_code == 200, r.text


def test_builder_me_lists_the_apps_the_caller_may_ship(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    cast = cast_of_builders(client, world, signing_key)
    ledger, mine = world.app, cast.owned

    def listed(user: str) -> set[str]:
        r = client.get(
            "/v1/apps",
            params={"builder": "me"},
            headers=auth(user_token(signing_key, world.org, user)),
        )
        assert r.status_code == 200, r.text
        return {a["id"] for a in r.json()["apps"]}

    def check(expected: dict[str, set[str]]) -> None:
        for name, user in cast.people.items():
            rule = asyncio.run(
                passes(dsns.app, world.org, user, authz.require_app_builder, [ledger, mine])
            )
            assert (name, listed(user)) == (name, expected[name]) == (name, rule)

    # The admin's list is exactly this org's two apps: other tests' orgs have apps too.
    first = {"admin": {ledger, mine}, "owner": {mine}, "direct": {ledger}, "grouped": {ledger}}
    check({**dict.fromkeys(cast.people, set[str]()), **first})

    open_owned_to_the_org(client, world, cast)
    check(
        {
            name: (set[str]() if name == "gone" else first.get(name, set()) | {mine})
            for name in cast.people
        }
    )

    nobody = user_token(signing_key, world.org, cast.people["nobody"])
    everyone = client.get("/v1/apps", headers=auth(nobody))
    assert {a["id"] for a in everyone.json()["apps"]} == {ledger, mine}
    admin = auth(world.admin_token)
    assert_problem(
        client.get("/v1/apps", params={"builder": "other"}, headers=admin),
        ErrorCode.VALIDATION_FAILED,
    )
    for kind in ("workload", "operator"):
        token = mint(signing_key, org=world.org, sub=world.admin, kind=kind)
        refused = client.get("/v1/apps", params={"builder": "me"}, headers=auth(token))
        assert_problem(refused, ErrorCode.FORBIDDEN)


def test_those_who_may_share_find_groups_by_name(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    cast = cast_of_builders(client, world, signing_key)

    def find(token: str, name: str | None) -> Response:
        params = {} if name is None else {"name": name}
        return client.get("/v1/groups", params=params, headers=auth(token))

    def group(w: World, ref: str, name: str, members: list[str]) -> str:
        r = sync_group(client, w, ref, name)
        assert r.status_code == 200, r.text
        gid = r.json()["group_id"]
        assert set_members(client, w, gid, members).status_code == 200
        return gid

    people = cast.people
    finance = group(world, "okta-00gfin", "Finance", [people["nobody"], people["gone"]])
    shouting = group(world, "okta-00gFIN2", "FINANCE", [])
    group(world, "okta-00gfin3", "Finance Team", [people["viewer"]])
    other_org, other_admin, _ = new_org(dsns.app)
    elsewhere = World(
        **{
            **world.__dict__,
            "org": other_org,
            "operator_token": internal_token(signing_key, other_org, "operator"),
        }
    )
    group(elsewhere, "okta-00gfin", "Finance", [])

    # Names are cached display data, not keys: every whole-name match, never another org's, and
    # only active members counted.
    found = find(world.admin_token, "finance")
    assert found.status_code == 200, found.text
    assert sorted(found.json()["groups"], key=lambda g: g["id"]) == sorted(
        [
            {"id": finance, "name": "Finance", "member_count": 1},
            {"id": shouting, "name": "FINANCE", "member_count": 0},
        ],
        key=lambda g: g["id"],
    )
    for miss in ("Financ", "Finance Tea", "%", "Financ_", "nobody"):
        assert find(world.admin_token, miss).json() == {"groups": []}
    for bad in (None, "", "x" * 201):
        assert_problem(find(world.admin_token, bad), ErrorCode.VALIDATION_FAILED)

    # Those who may change some app's sharing: an active admin (with no apps too), an owner, or
    # a builder on any environment; ``require_builder`` on some environment, checked person by
    # person. Everyone else, and any credential but a user's, is refused.
    other = find(user_token(signing_key, other_org, other_admin), "Finance")
    assert [g["member_count"] for g in other.json()["groups"]] == [0]
    envs = [world.prod, world.preview, cast.owned_prod, cast.owned_preview]

    def check(allowed: set[str]) -> None:
        for name, user in people.items():
            r = find(user_token(signing_key, world.org, user), "Finance")
            ok = r.status_code == 200
            if not ok:
                assert_problem(r, ErrorCode.FORBIDDEN)
            rule = name == "admin" or bool(
                asyncio.run(passes(dsns.app, world.org, user, authz.require_builder, envs))
            )
            assert (name, ok) == (name, name in allowed) == (name, rule)

    check({"admin", "owner", "direct", "grouped"})
    open_owned_to_the_org(client, world, cast)
    check(set(people) - {"gone"})
    for kind in ("workload", "operator"):
        token = mint(signing_key, org=world.org, sub=world.admin, kind=kind)
        assert_problem(find(token, "Finance"), ErrorCode.FORBIDDEN)


# ── one evaluator ────────────────────────────────────────────────────────────


def test_there_is_one_access_evaluator() -> None:
    pattern = re.compile(r"def decide\(([^)]*)\)", re.DOTALL)
    found = [
        path.relative_to(REPO).as_posix()
        for path in (REPO / "packages").rglob("*.py")
        if "/tests/" not in path.as_posix()
        for m in pattern.finditer(path.read_text())
        if "environment_id" in m.group(1)
    ]
    assert found == ["packages/ssc_shared/src/ssc_shared/access.py"]


# ── publishing ───────────────────────────────────────────────────────────────


class AbortError(Exception):
    pass


async def test_a_rolled_back_publish_leaves_latest_unchanged(dsns: Dsns, tmp_path: Path) -> None:
    org, _, _ = await asyncio.to_thread(new_org, dsns.app)
    blob = blob_store(tmp_path)
    v1, _ = await publish_now(dsns.app, org, blob)
    pointer = await read_blob(blob, latest_key(org))
    engine = make_engine(dsns.app)
    try:
        with pytest.raises(AbortError):
            async with bound_org(engine, org) as conn:
                assert await publish(conn, org, blob, at=datetime.now(UTC)) == v1 + 1
                raise AbortError
        assert await point_latest(engine, org, blob) == v1
    finally:
        await engine.dispose()
    assert await read_blob(blob, latest_key(org)) == pointer
    objects = sorted([info.key async for info in blob.list(f"snapshots/{org}/")])
    rows = await asyncio.to_thread(
        sql, dsns.app, org, "select version, object_key from ssc.access_snapshot"
    )
    assert [r["version"] for r in rows] == [v1]
    orphans = [k for k in objects if k not in {r["object_key"] for r in rows}]
    assert len(orphans) == 2  # latest.json and the rolled-back v2 object, never referenced
    assert latest_key(org) in orphans
    # The next publish takes v2 again, under its own content-addressed key.
    v2, doc = await publish_now(dsns.app, org, blob)
    assert v2 == v1 + 1
    assert json.loads(doc)["version"] == v2


# ── the compile job and acknowledgements ─────────────────────────────────────


def jobs_for(dsns: Dsns, org: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsns.superuser, row_factory=dict_row) as conn:
        return conn.execute(
            "select task_name, lock, args, status from procrastinate.procrastinate_jobs "
            "where queueing_lock = %s order by id",
            (compile_lock(org),),
        ).fetchall()


def test_sharing_changes_defer_one_coalesced_compile(
    client: TestClient, world: World, dsns: Dsns
) -> None:
    # Creating the app already asked for a compile; later changes join the waiting job.
    assert (
        put_grants(
            client, world, world.prod, world.admin_token, [grant("user", "org")], 1
        ).status_code
        == 200
    )
    new_member(client, world)
    (job,) = jobs_for(dsns, world.org)
    assert job == {
        "task_name": COMPILE_TASK,
        "lock": compile_lock(world.org),
        "args": {"org_id": world.org},
        "status": "todo",
    }
    # Re-sending the same grants changes nothing and asks for nothing more.
    assert (
        put_grants(
            client, world, world.prod, world.admin_token, [grant("user", "org")], 2
        ).status_code
        == 200
    )


async def test_the_worker_compiles_and_moves_latest(dsns: Dsns, tmp_path: Path) -> None:
    name = f"s{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    app_dsn = make_url(dsns.app).set(database=name).render_as_string(hide_password=False)
    await asyncio.to_thread(
        upgrade, make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)
    )
    org, _, _ = await asyncio.to_thread(new_org, app_dsn)
    engine = make_engine(app_dsn)
    blob = blob_store(tmp_path)
    try:
        async with bound_org(engine, org) as conn:
            assert await mark_dirty(conn, org) == 1
        app = App(connector=PsycopgConnector(conninfo=queue_conninfo(app_dsn)))
        app.add_tasks_from(snapshot_jobs.blueprint(), namespace="snapshot")
        ports = Ports(engine=engine, blob_store=blob)
        async with app.open_async():
            await app.run_worker_async(
                additional_context={PORTS_KEY: ports}, wait=False, install_signal_handlers=False
            )
        async with bound_org(engine, org) as conn:
            assert await Snapshots(engine).request(conn, org) == 2
    finally:
        await engine.dispose()
    pointer = json.loads(await read_blob(blob, latest_key(org)))
    assert pointer["version"] == 1
    assert json.loads(await read_blob(blob, pointer["key"]))["org_id"] == org
    assert COMPILE_TASK in build_app(app_dsn).tasks


async def run_snapshot_worker(app_dsn: str, ports: Ports, *, sweep: bool) -> None:
    """One worker pass over the queue, after deferring a sweep when ``sweep``."""
    app = App(connector=PsycopgConnector(conninfo=queue_conninfo(app_dsn)))
    app.add_tasks_from(snapshot_jobs.blueprint(), namespace="snapshot")
    async with app.open_async():
        if sweep:
            now = int(datetime.now(UTC).timestamp())
            await app.configure_task("snapshot:stale_sweep").defer_async(timestamp=now)
        await app.run_worker_async(
            additional_context={PORTS_KEY: ports}, wait=False, install_signal_handlers=False
        )


def compile_jobs(superuser_dsn: str) -> list[tuple[str, str]]:
    with psycopg.connect(superuser_dsn) as conn:
        return [
            (str(org), str(status))
            for org, status in conn.execute(
                "select args->>'org_id', status from procrastinate.procrastinate_jobs "
                "where task_name = %s order by 1, 2",
                (COMPILE_TASK,),
            )
        ]


async def test_the_sweep_recompiles_only_the_snapshots_that_lag(dsns: Dsns, tmp_path: Path) -> None:
    name = f"s{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    at = {
        r: make_url(d).set(database=name).render_as_string(hide_password=False)
        for r, d in (("app", dsns.app), ("migrate", dsns.migrate), ("superuser", dsns.superuser))
    }
    await asyncio.to_thread(upgrade, at["migrate"])
    current, never, behind, drifted = [
        (await asyncio.to_thread(new_org, at["app"]))[0] for _ in range(4)
    ]
    blob = blob_store(tmp_path)
    for org in (current, behind, drifted):
        await publish_now(at["app"], org, blob)
    engine = make_engine(at["app"])
    try:
        async with bound_org(engine, behind) as conn:  # v2 committed, its pointer never moved
            await publish(conn, behind, blob, at=datetime.now(UTC))
        async with bound_org(engine, drifted) as conn:  # a change that skipped mark_dirty
            await conn.execute(
                text(
                    "insert into ssc.user_account (id, org_id, display_name, email, role) "
                    "values (:id, :org, 'New Hire', 'new@example.com', 'member')"
                ),
                {"id": new_id("usr"), "org": drifted},
            )
        # With no blob store the sweep marks nothing, and a compile does nothing but succeed.
        async with bound_org(engine, never) as conn:
            await mark_dirty(conn, never)
        await run_snapshot_worker(at["app"], Ports(engine=engine), sweep=True)
        assert compile_jobs(at["superuser"]) == [(never, "succeeded")]
        await run_snapshot_worker(at["app"], Ports(engine=engine, blob_store=blob), sweep=True)
    finally:
        await engine.dispose()
    assert compile_jobs(at["superuser"]) == sorted(
        [(never, "succeeded"), (never, "succeeded"), (behind, "succeeded"), (drifted, "succeeded")]
    )
    pointers = {
        org: json.loads(await read_blob(blob, latest_key(org)))["version"]
        for org in (current, never, behind, drifted)
    }
    assert pointers == {current: 1, never: 1, behind: 3, drifted: 2}


async def test_each_orgs_snapshot_goes_to_its_own_cell_and_the_cell_applies_it(
    dsns: Dsns, tmp_path: Path
) -> None:
    """SSC-013: with ``cell_stores`` the compile and the sweep use the org's cell bucket, and the
    cell's feed loads what the worker wrote."""
    name = f"s{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    at = {
        r: make_url(d).set(database=name).render_as_string(hide_password=False)
        for r, d in (("app", dsns.app), ("migrate", dsns.migrate), ("superuser", dsns.superuser))
    }
    await asyncio.to_thread(upgrade, at["migrate"])
    first, second = [(await asyncio.to_thread(new_org, at["app"]))[0] for _ in range(2)]
    labels = {
        org: sql(at["app"], org, "select cell_label from ssc.org")[0]["cell_label"]
        for org in (first, second)
    }
    buckets: dict[str, FsBlobStore] = {}

    def cell_store(label: str) -> FsBlobStore:
        return buckets.setdefault(label, blob_store(tmp_path / label))

    engine = make_engine(at["app"])
    try:
        async with bound_org(engine, first) as conn:
            await mark_dirty(conn, first)
        ports = Ports(engine=engine, cell_stores=cell_store)
        await run_snapshot_worker(at["app"], ports, sweep=False)
        await run_snapshot_worker(at["app"], ports, sweep=True)
    finally:
        await engine.dispose()
    assert compile_jobs(at["superuser"]) == sorted([(first, "succeeded"), (second, "succeeded")])
    assert set(buckets) == {labels[first], labels[second]}
    for org in (first, second):
        mine, other = buckets[labels[org]], buckets[labels[second if org == first else first]]
        assert [i.key async for i in other.list(f"snapshots/{org}/")] == []
        feed = SnapshotFeed(mine, ViewHolder(org))
        assert await feed.poll_once() is True
        assert feed.version == 1


def heartbeat(client: TestClient, w: World, cell: str, version: int | None) -> Response:
    return client.post(
        "/internal/v1/heartbeat",
        json={"cell_label": cell, "snapshot_version": version},
        headers=auth(w.workload_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def test_heartbeats_acknowledge_published_versions(
    client: TestClient, world: World, dsns: Dsns, tmp_path: Path
) -> None:
    async def confirmed(version: int) -> bool:
        engine = make_engine(dsns.app)
        try:
            return await Snapshots(engine).confirmed(world.org, version)
        finally:
            await engine.dispose()

    with psycopg.connect(dsns.superuser) as conn:
        row = conn.execute("select cell_label from ssc.org where id = %s", (world.org,)).fetchone()
    assert row is not None
    label = row[0]
    assert asyncio.run(confirmed(1)) is False
    version, _ = asyncio.run(publish_now(dsns.app, world.org, blob_store(tmp_path)))
    assert_problem(heartbeat(client, world, label, version + 5), ErrorCode.REFERENCE_NOT_FOUND)
    assert heartbeat(client, world, label, version).status_code == 200
    assert asyncio.run(confirmed(version)) is True
    assert asyncio.run(confirmed(version + 1)) is False
    # Another cell's heartbeat is refused; once the org moves to another cell, the old ack does
    # not count until the new cell reports.
    assert_problem(heartbeat(client, world, CELL, version), ErrorCode.FORBIDDEN)
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute("update ssc.org set cell_label = 'cellwxyz' where id = %s", (world.org,))
    assert asyncio.run(confirmed(version)) is False
    assert heartbeat(client, world, "cellwxyz", version).status_code == 200
    assert asyncio.run(confirmed(version)) is True


# ── migration 0009 ───────────────────────────────────────────────────────────


def test_0009_downgrades_and_upgrades(dsns: Dsns) -> None:
    rev = importlib.import_module("ssc_control.db.migrations.versions.0009_access_snapshot")
    name = f"m{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)
    tables = (
        "select count(*) from pg_tables where schemaname = 'ssc' "
        "and tablename in ('access_snapshot', 'snapshot_ack')"
    )

    def count() -> int:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(tables).fetchone()
        assert row is not None
        return int(row[0])

    upgrade(dsn)
    assert count() == 2
    downgrade(dsn, rev.down_revision)
    assert count() == 0
    upgrade(dsn)
    assert count() == 2
