"""Every app has a named owner (decision 019 amendment 2026-10-29): ``owner_state``, the
snapshot's ``owner_needed``, the gateway's page and the admins' mail.

Success criteria (``docs/plans/app-named-owner-2026-10-28.md``):
  1. a new app is its builder's, active -> test_a_new_app_is_owned_by_its_builder_and_active
  2. owner deactivated: ownerless, opens, admins told once
        -> test_deactivating_the_owner_leaves_the_app_ownerless_and_tells_each_admin_once
  3. owner and every admin gone: blocked, 403 on prod and preview, the data gateway refuses, a
     disabled app stays disabled -> test_with_no_active_owner_or_admin_the_app_is_blocked
  4. an admin assigns an active person, the next snapshot serves it; inactive or unknown refused
        -> test_assigning_an_active_owner_unblocks_the_app_in_the_next_snapshot
  5. the old owner reactivated -> test_reactivating_the_old_owner_makes_the_app_active
  6. agents, CI tokens and members cannot assign -> test_only_an_admins_own_credential_assigns
  7. an old reader denies -> ssc_shared's
     test_a_reader_from_before_owner_needed_refuses_the_document, and ssc_edge's
     test_an_app_that_needs_an_owner_answers_403_on_prod_and_preview
Plus: a role change marks the snapshot dirty, and the mail is dropped once the app has an owner.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, mint, new_key
from test_access import (
    World,
    apps_of,
    create_app_as,
    grant,
    internal_token,
    new_org,
    on,
    pg,
    put_grants,
    sql,
    sync_user,
    user_token,
)

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.snapshot import SnapshotDoc
from ssc_control.api import Settings, create_app
from ssc_control.audit import chain
from ssc_control.db import bind_org_sync, bound_org, make_engine
from ssc_control.lifecycle import kill_switch
from ssc_control.notifications import delivery
from ssc_control.notifications.mailer import LogMailer
from ssc_control.notifications.service import owner_left
from ssc_control.snapshot.compiler import compile_document
from ssc_control.snapshot.service import COMPILE_TASK
from ssc_datagw.admission import environment_refusal
from ssc_edge.gate import Allow, Deny, Facts, GateConfig
from ssc_edge.keys import new_keyring, parse_keyring
from ssc_edge.server import gate_for
from ssc_edge.session import Session, SessionCodec, new_sid
from ssc_shared.access import AccessView, decide

CONSOLE = "https://console.example.test"
DOMAIN = "apps.test"


# ── the world ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client(dsns: Dsns, signing_key: SigningKey) -> Iterator[TestClient]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
    )
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def world(client: TestClient, dsns: Dsns, signing_key: SigningKey) -> World:
    """An org whose admin, Ada, owns ledger."""
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


def member(client: TestClient, w: World, name: str, role: str = "member") -> tuple[str, str]:
    """A person synced from the directory; their user id and identity subject."""
    subject = f"sub-{uuid.uuid4().hex}"
    r = sync_user(client, w, subject, role=role, name=name, email=f"{name.lower()}@example.com")
    assert r.status_code == 200, r.text
    return r.json()["user_id"], subject


def set_status(
    client: TestClient, w: World, subject: str, name: str, status: str, **kw: str
) -> None:
    email = f"{name.lower()}@example.com"
    r = sync_user(client, w, subject, status=status, name=name, email=email, **kw)
    assert r.status_code == 200, r.text


def app_of(client: TestClient, token: str, app_id: str) -> dict[str, Any]:
    r = client.get(f"/v1/apps/{app_id}", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def inventory_states(client: TestClient, w: World, token: str | None = None) -> dict[str, str]:
    r = client.get("/v1/inventory", headers=auth(token or w.admin_token))
    assert r.status_code == 200, r.text
    return {a["slug"]: a["owner_state"] for a in r.json()["items"]}


def transfer(client: TestClient, w: World, user_id: str, token: str) -> Any:
    return client.put(f"/v1/apps/{w.app}/owner", json={"user_id": user_id}, headers=auth(token))


def compiled(dsn: str, org: str) -> SnapshotDoc:
    async def go() -> SnapshotDoc:
        engine = make_engine(dsn)
        try:
            async with bound_org(engine, org) as conn:
                return await compile_document(conn, org, version=1, compiled_at=datetime.now(UTC))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def statuses(doc: SnapshotDoc, w: World) -> tuple[str, str]:
    return doc.environments[w.prod].status, doc.environments[w.preview].status


def ownerless_mail(dsn: str, org: str) -> list[dict[str, Any]]:
    return sql(
        dsn,
        org,
        "select user_id, app_id, approval_id, state from ssc.notification_outbox "
        "where kind = 'app_ownerless' order by user_id, created_at",
    )


def flush(dsn: str, org: str) -> LogMailer:
    sender = LogMailer()

    async def go() -> None:
        engine = make_engine(dsn)
        try:
            await delivery.flush(engine, sender, org_id=org, console_url=CONSOLE)
        finally:
            await engine.dispose()

    asyncio.run(go())
    return sender


def drain_compiles(dsns: Dsns, org: str) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute("set search_path = procrastinate, public")
        conn.execute(
            "delete from procrastinate.procrastinate_jobs where task_name = %s "
            "and args->>'org_id' = %s",
            (COMPILE_TASK, org),
        )


def compiles_queued(dsns: Dsns, org: str) -> int:
    with psycopg.connect(dsns.superuser) as conn:
        row = conn.execute(
            "select count(*) from procrastinate.procrastinate_jobs where task_name = %s "
            "and args->>'org_id' = %s and status = 'todo'",
            (COMPILE_TASK, org),
        ).fetchone()
    assert row is not None
    return int(row[0])


def share(client: TestClient, w: World, user: str) -> None:
    """``user`` may open prod (a user grant) and preview (a builder grant)."""
    for env, role in ((w.prod, "user"), (w.preview, "builder")):
        current = client.get(
            f"/v1/apps/{w.app}/environments/{env}/grants", headers=auth(w.admin_token)
        )
        assert current.status_code == 200, current.text
        body = current.json()
        kept = [grant(g["role"], g["subject_kind"], g["subject_id"]) for g in body["grants"]]
        r = put_grants(
            client,
            w,
            env,
            w.admin_token,
            [*kept, grant(role, "user", user)],
            body["grants_version"],
        )
        assert r.status_code == 200, r.text


def gate_answers(dsn: str, w: World, doc: SnapshotDoc, user: str) -> list[Allow | Deny]:
    """What the gateway answers ``user`` on the app's prod and preview hosts from ``doc``."""
    (row,) = sql(dsn, w.org, "select cell_label from ssc.org where id = %s", (w.org,))
    label = str(row["cell_label"])
    keyring = parse_keyring(new_keyring())
    config = GateConfig(
        org_id=w.org,
        cell_label=label,
        apps_domain=DOMAIN,
        auth_url="https://auth.example.test",
        issuer=f"https://keys.example.test/{label}",
        project_number="123456789012",
        region="us-central1",
        max_body_bytes=1024,
    )
    view = AccessView(doc)
    gate = gate_for(config, keyring, view=lambda: view)
    ((slug,),) = [
        r.values() for r in sql(dsn, w.org, "select slug from ssc.app where id = %s", (w.app,))
    ]
    now = int(time.time())
    who = Session(
        sid=new_sid(), sub=user, org=w.org, name="Una", email="una@example.com",
        iat=now - 60, exp=now + 3600,
    )  # fmt: skip
    codec = SessionCodec(keyring.session, active=keyring.session_kid)
    answers: list[Allow | Deny] = []
    for host in (f"{slug}.{label}.{DOMAIN}", f"{slug}--preview.{label}.{DOMAIN}"):
        cookie = f"__Host-ssc-session={codec.seal(who, host)}"
        facts = Facts(method="GET", host=host, path="/", headers={"cookie": cookie})
        answers.append(asyncio.run(gate.check(facts)))
    return answers


def owner_needed_page(answers: list[Allow | Deny]) -> bool:
    return all(
        isinstance(a, Deny) and (a.status, a.reason) == (403, "owner_needed") for a in answers
    )


# ── criteria ─────────────────────────────────────────────────────────────────


def test_a_new_app_is_owned_by_its_builder_and_active(
    client: TestClient, world: World, signing_key: SigningKey
) -> None:
    bo, _ = member(client, world, "Bo")
    bo_token = user_token(signing_key, world.org, bo)
    atlas = create_app_as(client, bo_token, "atlas")
    assert (atlas["owner_user_id"], atlas["owner_state"]) == (bo, "active")
    assert app_of(client, bo_token, atlas["id"])["owner_state"] == "active"
    assert inventory_states(client, world) == {"atlas": "active", "ledger": "active"}
    mine = apps_of(client, bo_token).json()["apps"]
    assert [(a["slug"], a["owner_state"]) for a in mine] == [("atlas", "active")]


def test_deactivating_the_owner_leaves_the_app_ownerless_and_tells_each_admin_once(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    cy, _ = member(client, world, "Cy", role="admin")
    bo, bo_subject = member(client, world, "Bo")
    w = on(world, create_app_as(client, user_token(signing_key, world.org, bo), "atlas"))
    share(client, w, world.admin)

    set_status(client, w, bo_subject, "Bo", "deactivated")
    assert app_of(client, w.admin_token, w.app)["owner_state"] == "ownerless"
    assert inventory_states(client, w) == {"atlas": "ownerless", "ledger": "active"}
    doc = compiled(dsns.app, w.org)
    assert statuses(doc, w) == ("active", "active")
    assert decide(AccessView(doc), w.prod, world.admin).allowed
    assert all(isinstance(a, Allow) for a in gate_answers(dsns.app, w, doc, world.admin))

    mail = ownerless_mail(dsns.app, w.org)
    assert [(m["user_id"], m["app_id"], m["approval_id"]) for m in mail] == sorted(
        [(world.admin, w.app, None), (cy, w.app, None)]
    )

    async def again() -> int:
        engine = make_engine(dsns.app)
        try:
            async with bound_org(engine, w.org) as conn:
                return await owner_left(conn, org_id=w.org, owner_id=bo)
        finally:
            await engine.dispose()

    assert asyncio.run(again()) == 0
    set_status(client, w, bo_subject, "Bo", "deactivated", role="member")
    assert len(ownerless_mail(dsns.app, w.org)) == 2  # noqa: PLR2004

    sender = flush(dsns.app, w.org)
    assert sorted(m.to for m in sender.sent) == ["ada@example.com", "cy@example.com"]
    for m in sender.sent:
        assert m.subject == "atlas has no owner"
        assert m.body == f"atlas has no owner. Assign one: {CONSOLE}/apps/{w.app}"
    assert {m["state"] for m in ownerless_mail(dsns.app, w.org)} == {"sent"}


def test_with_no_active_owner_or_admin_the_app_is_blocked(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    una, _ = member(client, world, "Una")
    una_token = user_token(signing_key, world.org, una)
    share(client, world, una)
    assert all(
        isinstance(a, Allow)
        for a in gate_answers(dsns.app, world, compiled(dsns.app, world.org), una)
    )

    # Social orgs refuse deactivating the last admin; the directory may (SSC-019).
    set_status(client, world, world.admin_subject, "Ada", "deactivated", role="admin")
    assert app_of(client, una_token, world.app)["owner_state"] == "blocked"
    (listed,) = apps_of(client, una_token).json()["apps"]
    assert (listed["slug"], listed["owner_state"]) == ("ledger", "blocked")
    assert [e["name"] for e in listed["environments"]] == ["prod", "preview"]

    doc = compiled(dsns.app, world.org)
    assert statuses(doc, world) == ("owner_needed", "owner_needed")
    assert owner_needed_page(gate_answers(dsns.app, world, doc, una))
    view = AccessView(doc)
    assert environment_refusal(view, world.prod) == "APP_NOT_ACTIVE"
    assert environment_refusal(view, world.preview) == "APP_NOT_ACTIVE"
    assert ownerless_mail(dsns.app, world.org) == []  # no admin is left to tell

    sql(dsns.app, world.org, "update ssc.app set status = 'disabled' where id = %s", (world.app,))
    doc = compiled(dsns.app, world.org)
    assert statuses(doc, world) == ("disabled", "disabled")
    for answer in gate_answers(dsns.app, world, doc, una):
        assert isinstance(answer, Deny)
        assert (answer.status, answer.reason) == (404, "not_granted")


def test_assigning_an_active_owner_unblocks_the_app_in_the_next_snapshot(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    una, _ = member(client, world, "Una")
    share(client, world, una)
    set_status(client, world, world.admin_subject, "Ada", "deactivated", role="admin")
    assert statuses(compiled(dsns.app, world.org), world) == ("owner_needed", "owner_needed")

    # An operator, or the directory, gives the org an admin again: the app is ownerless.
    drain_compiles(dsns, world.org)
    nell, _ = member(client, world, "Nell", role="admin")
    assert compiles_queued(dsns, world.org) == 1
    nell_token = user_token(signing_key, world.org, nell)
    assert app_of(client, nell_token, world.app)["owner_state"] == "ownerless"
    assert statuses(compiled(dsns.app, world.org), world) == ("active", "active")

    assert_problem(transfer(client, world, world.admin, nell_token), ErrorCode.OWNER_NOT_ACTIVE)
    assert_problem(
        transfer(client, world, new_id("usr"), nell_token), ErrorCode.REFERENCE_NOT_FOUND
    )
    drain_compiles(dsns, world.org)
    r = transfer(client, world, una, nell_token)
    assert r.status_code == 200, r.text
    assert (r.json()["owner_user_id"], r.json()["owner_state"]) == (una, "active")
    assert compiles_queued(dsns, world.org) == 1
    doc = compiled(dsns.app, world.org)
    assert statuses(doc, world) == ("active", "active")
    assert all(isinstance(a, Allow) for a in gate_answers(dsns.app, world, doc, una))


def test_a_role_change_marks_the_snapshot_dirty(
    client: TestClient, world: World, dsns: Dsns
) -> None:
    una, una_subject = member(client, world, "Una")
    share(client, world, una)
    set_status(client, world, world.admin_subject, "Ada", "deactivated", role="admin")
    drain_compiles(dsns, world.org)
    set_status(client, world, una_subject, "Una", "active", role="admin")
    assert compiles_queued(dsns, world.org) == 1
    assert statuses(compiled(dsns.app, world.org), world) == ("active", "active")


def test_reactivating_the_old_owner_makes_the_app_active(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    bo, bo_subject = member(client, world, "Bo")
    w = on(world, create_app_as(client, user_token(signing_key, world.org, bo), "atlas"))
    set_status(client, w, bo_subject, "Bo", "deactivated")
    assert app_of(client, w.admin_token, w.app)["owner_state"] == "ownerless"
    set_status(client, w, bo_subject, "Bo", "active")
    assert app_of(client, w.admin_token, w.app)["owner_state"] == "active"
    set_status(client, w, bo_subject, "Bo", "deactivated")
    assert len(ownerless_mail(dsns.app, w.org)) == 2  # noqa: PLR2004  (once per time)

    set_status(client, world, world.admin_subject, "Ada", "deactivated", role="admin")
    assert statuses(compiled(dsns.app, w.org), world) == ("owner_needed", "owner_needed")
    set_status(client, world, world.admin_subject, "Ada", "active", role="admin")
    assert app_of(client, world.admin_token, world.app)["owner_state"] == "active"
    assert statuses(compiled(dsns.app, w.org), world) == ("active", "active")


def test_only_an_admins_own_credential_assigns(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    bo, bo_subject = member(client, world, "Bo")
    una, _ = member(client, world, "Una")
    w = on(world, create_app_as(client, user_token(signing_key, world.org, bo), "atlas"))
    set_status(client, w, bo_subject, "Bo", "deactivated")
    jti = f"cred_{new_key()[:16]}"
    agent = mint(signing_key, org=w.org, sub=world.admin, jti=jti, agent=True)
    ci = mint(
        signing_key, org=w.org, sub=world.admin, jti=f"cred_{new_key()[:16]}", scope="preview"
    )
    refusals = [
        (agent, ErrorCode.AGENT_SESSION_REFUSED),
        (ci, ErrorCode.FORBIDDEN),
        (user_token(signing_key, w.org, una), ErrorCode.FORBIDDEN),
        (internal_token(signing_key, w.org, "operator"), ErrorCode.UNAUTHENTICATED),
    ]
    for token, code in refusals:
        assert_problem(transfer(client, w, una, token), code)
    assert app_of(client, w.admin_token, w.app)["owner_state"] == "ownerless"


def test_the_mail_is_dropped_once_the_app_has_an_owner(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    bo, bo_subject = member(client, world, "Bo")
    una, _ = member(client, world, "Una")
    w = on(world, create_app_as(client, user_token(signing_key, world.org, bo), "atlas"))
    set_status(client, w, bo_subject, "Bo", "deactivated")
    assert transfer(client, w, una, w.admin_token).status_code == 200
    assert flush(dsns.app, w.org).sent == type(LogMailer().sent)()
    assert ownerless_mail(dsns.app, w.org) == []


def test_deactivating_the_owner_while_the_kill_switch_holds_the_app_does_not_deadlock(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    bo, bo_subject = member(client, world, "Bo")
    w = on(world, create_app_as(client, user_token(signing_key, world.org, bo), "atlas"))
    with ThreadPoolExecutor(1) as pool, psycopg.connect(dsns.app) as switch:
        bind_org_sync(switch, w.org)
        switch.execute("set lock_timeout = '5s'")
        switch.execute(pg(kill_switch._LOCK_APP), {"org": w.org, "app": w.app})
        left = pool.submit(set_status, client, w, bo_subject, "Bo", "deactivated")
        wait([left], timeout=3)
        switch.execute(pg(chain._LOCK_HEAD), {"org": w.org})
        switch.commit()
        left.result(timeout=10)
    assert app_of(client, w.admin_token, w.app)["owner_state"] == "ownerless"
    assert [m["app_id"] for m in ownerless_mail(dsns.app, w.org)] == [w.app]
