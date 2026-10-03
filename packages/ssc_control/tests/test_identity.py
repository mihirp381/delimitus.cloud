"""SSC-019: directory sync, joining a login to a person, sessions, codes and command-line tokens,
against a real postgres:18 and a fake WorkOS (decision 024).

Ticket "done when" checks:
  * an email change does not create a second user -> test_an_email_change_keeps_the_person
  * a deactivated or removed user is locked out -> test_deactivation_revokes_everything_they_hold,
        test_removal_from_the_directory_deactivates and
        test_deactivation_reaches_the_snapshot (the gateway half is ssc_edge's
        test_a_session_from_before_a_revocation_is_sent_back_to_login)
  * login with the Okta and Google Workspace tenants: the rules here; the live tenants are the
        runbook in docs/runbooks/ssc-019-login.md

SSC-021 "a user removed from a group loses access on the next request after the snapshot update
and their open session is dropped", from the directory to the gateway ->
test_removal_from_a_group_reaches_the_gateway_and_closes_the_open_stream
"""

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fake_workos import FakeWorkOS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor
from ssc_control.db import NewOrg, bound_org, create_org, make_engine
from ssc_control.identity import connections, join, sessions, sync, tokens
from ssc_control.identity.connections import ConnectError, directory_issuer
from ssc_control.identity.rules import JoinRule, SsoProfile
from ssc_control.snapshot.compiler import compile_document, point_latest, publish
from ssc_control.snapshot.service import compile_lock
from ssc_edge.gate import STREAM_HEADER, Allow, Deny, Facts, GateConfig
from ssc_edge.keys import new_keyring, parse_keyring
from ssc_edge.server import RECHECK_SECONDS, OnDemandView, gate_for
from ssc_edge.session import Session, SessionCodec, new_sid
from ssc_edge.streams import WATCH_SECONDS, Streams
from ssc_shared.access import AccessView, ViewHolder, decide
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock
from ssc_shared.snapshot_feed import SnapshotFeed

OPERATOR = Actor(kind=ActorKind.OPERATOR, id="op_test")
FOUNDER_UID, FOUNDER_IDP = "directory_user_01FOUNDER", "00ufounder"
BOB_UID, BOB_IDP = "directory_user_01BOB", "00ubob"


@dataclass
class World:
    engine: AsyncEngine
    org: str
    founder: str
    wo: FakeWorkOS

    async def tick(self) -> sync.TickReport | None:
        client = self.wo.client()
        try:
            return await sync.tick(self.engine, client, self.org)
        finally:
            await client.aclose()

    async def connection(self) -> connections.DirectoryConnection:
        async with bound_org(self.engine, self.org) as conn:
            found = await connections.load(conn, self.org)
        assert found is not None
        return found

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with bound_org(self.engine, self.org) as conn:
            result = await conn.execute(text(sql), {"org": self.org, **params})
            return list(result.all()) if result.returns_rows else []

    async def person(self, subject: str) -> tuple[str, str, str, str]:
        """``(user id, email, status, role)`` of the person the directory keys by ``subject``."""
        (row,) = await self.rows(
            "select u.id, u.email, u.status, u.role from ssc.identity_link l "
            "join ssc.user_account u on u.org_id = l.org_id and u.id = l.user_id "
            "where l.org_id = :org and l.issuer = :issuer and l.subject = :subject",
            issuer=directory_issuer(self.wo.directory),
            subject=subject,
        )
        return str(row[0]), str(row[1]), str(row[2]), str(row[3])

    async def user_count(self) -> int:
        return int(
            (await self.rows("select count(*) from ssc.user_account where org_id = :org"))[0][0]
        )

    async def full_sync_due(self) -> None:
        await self.rows(
            "update ssc.directory_connection set last_full_sync_at = now() - interval '7 hours' "
            "where org_id = :org"
        )

    async def find(self, profile: SsoProfile) -> join.Person | str:
        async with bound_org(self.engine, self.org) as conn:
            return await join.find_person(conn, await connections.load(conn, self.org), profile)  # type: ignore[arg-type]

    async def open(self, user: str, kind: sessions.SessionKind = "browser") -> str:
        async with bound_org(self.engine, self.org) as conn:
            return await sessions.open_session(
                conn,
                self.org,
                user_id=user,
                kind=kind,
                connection_id=self.wo.sso,
                actor=Actor(ActorKind.USER, user),
            )

    async def live(self, sid: str) -> sessions.LiveSession | None:
        async with bound_org(self.engine, self.org) as conn:
            return await sessions.live_session(conn, self.org, sid)

    async def audit(self, action: AuditAction) -> list[Any]:
        return await self.rows(
            "select target_kind, target_id, after from ssc.audit_event "
            "where org_id = :org and action = :action order by seq",
            action=action.value,
        )


async def new_world(
    dsns: Dsns, *, join_rule: JoinRule = "idp_id", founder_idp: str = FOUNDER_IDP, **kw: Any
) -> World:
    engine = make_engine(dsns.app)
    wo = FakeWorkOS()
    wo.user(FOUNDER_UID, founder_idp, "ada@example.com", first="Ada", last="Admin")
    created = await create_org(
        engine,
        NewOrg("Acme", "Ada Admin", "ada@example.com", directory_issuer(wo.directory), founder_idp),
    )
    async with bound_org(engine, created.org_id) as conn:
        await connections.connect(
            conn,
            created.org_id,
            workos_organization_id=wo.organization,
            workos_directory_id=wo.directory,
            sso_connection_ids=[wo.sso],
            join_rule=join_rule,
            admin_group_ref=kw.get("admin_group_ref"),
            actor=OPERATOR,
        )
    return World(engine, created.org_id, created.admin_user_id, wo)


@pytest.fixture
async def world(dsns: Dsns) -> AsyncIterator[World]:
    w = await new_world(dsns)
    try:
        yield w
    finally:
        await w.engine.dispose()


def profile(wo: FakeWorkOS, idp_id: str, email: str, **kw: str) -> SsoProfile:
    wo.profile("c", idp_id, email, **kw)
    return SsoProfile.from_wire(wo.profiles["c"])


# ── connecting ───────────────────────────────────────────────────────────────


async def test_connecting_needs_the_founder_under_the_directory_issuer(dsns: Dsns) -> None:
    engine = make_engine(dsns.app)
    try:
        created = await create_org(
            engine, NewOrg("Acme", "Ada", "ada@example.com", "https://elsewhere.test", "s-1")
        )
        async with bound_org(engine, created.org_id) as conn:
            for sso in ([], ["conn_01X"]):
                with pytest.raises(ConnectError):
                    await connections.connect(
                        conn,
                        created.org_id,
                        workos_organization_id="org_01X",
                        workos_directory_id="directory_01X",
                        sso_connection_ids=sso,
                        join_rule="idp_id",
                        admin_group_ref=None,
                        actor=OPERATOR,
                    )
    finally:
        await engine.dispose()


# ── directory sync ───────────────────────────────────────────────────────────


async def test_the_first_sync_finds_the_founder_and_adds_the_directory(world: World) -> None:
    wo = world.wo
    wo.event("dsync.user.created", {"id": "old", "directory_id": wo.directory})
    wo.user(BOB_UID, BOB_IDP, "bob@example.com", first="Bob")
    wo.user("directory_user_01CAROL", "00ucarol", "carol@example.com", state="inactive")
    wo.user(
        "directory_user_01GUEST",
        "00uguest",
        "g@partner.test",
        custom_attributes={"userType": "Guest"},
    )
    wo.user("directory_user_01ADDR", "eve@example.com", "eve@example.com")
    wo.user("directory_user_01NOSUB", "", "nosub@example.com")
    wo.group("directory_group_01FIN", "Finance", FOUNDER_UID, BOB_UID, "directory_user_01GUEST")

    report = await world.tick()
    assert report is not None and report.full and report.people == 3 and report.error is None
    assert (await world.person(FOUNDER_IDP))[0] == world.founder
    assert (await world.person(BOB_IDP))[1:] == ("bob@example.com", "active", "member")
    assert (await world.person("00ucarol"))[2] == "deactivated"
    assert await world.user_count() == 3
    groups = await world.rows(
        "select g.directory_ref, g.display_name, array_agg(m.user_id order by m.user_id) "
        "from ssc.user_group g join ssc.group_member m "
        "on m.org_id = g.org_id and m.group_id = g.id "
        "where g.org_id = :org group by g.id"
    )
    bob = (await world.person(BOB_IDP))[0]
    assert groups == [("directory_group_01FIN", "Finance", sorted([world.founder, bob]))]
    connection = await world.connection()
    assert connection.event_cursor == "event_000001"
    assert await world.tick() == sync.TickReport(world.org)


async def test_an_email_change_keeps_the_person(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com", first="Bob")
    await world.tick()
    bob = await world.person(BOB_IDP)
    world.wo.user(BOB_UID, BOB_IDP, "robert@example.com", first="Robert")
    world.wo.user_event("dsync.user.updated", BOB_UID)
    report = await world.tick()
    assert report is not None and not report.full and report.people == 1
    assert await world.person(BOB_IDP) == (bob[0], "robert@example.com", "active", "member")
    assert await world.user_count() == 2
    names = await world.rows(
        "select display_name from ssc.user_account where org_id = :org and id = :id", id=bob[0]
    )
    assert names == [("Robert Doe",)]


async def test_an_address_handed_to_someone_new_is_a_different_person(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "sales@example.com")
    await world.tick()
    world.wo.user(BOB_UID, BOB_IDP, "sales@example.com", state="inactive")
    world.wo.user("directory_user_01DAN", "00udan", "sales@example.com", first="Dan")
    world.wo.user_event("dsync.user.updated", BOB_UID)
    world.wo.user_event("dsync.user.created", "directory_user_01DAN")
    await world.tick()
    bob, dan = await world.person(BOB_IDP), await world.person("00udan")
    assert bob[0] != dan[0]
    assert (bob[2], dan[2]) == ("deactivated", "active")


async def test_deactivation_revokes_everything_they_hold(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    await world.tick()
    bob = (await world.person(BOB_IDP))[0]
    browser, cli = await world.open(bob), await world.open(bob, "cli")
    async with bound_org(world.engine, world.org) as conn:
        refresh = await tokens.issue_refresh(conn, world.org, cli)
    assert await world.live(browser) is not None

    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com", state="inactive")
    world.wo.user_event("dsync.user.updated", BOB_UID)
    await world.tick()

    assert await world.live(browser) is None and await world.live(cli) is None
    async with bound_org(world.engine, world.org) as conn:
        assert await tokens.rotate_refresh(conn, world.org, refresh, actor=OPERATOR) is None
    revoked = await world.audit(AuditAction.TOKEN_REVOKED)
    assert sorted(r[1] for r in revoked) == sorted([browser, cli])
    assert all(r[2] == {"reason": "user_deactivated"} for r in revoked)
    (nb,) = await world.rows(
        "select sessions_not_before from ssc.user_account where org_id = :org and id = :id", id=bob
    )
    assert nb[0] is not None

    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    world.wo.user_event("dsync.user.updated", BOB_UID)
    await world.tick()
    assert (await world.person(BOB_IDP))[2] == "active"
    assert await world.live(browser) is None, "reactivation does not revive old sessions"
    assert await world.live(await world.open(bob)) is not None


async def test_removal_from_the_directory_deactivates(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    await world.tick()
    sid = await world.open((await world.person(BOB_IDP))[0])
    del world.wo.users[BOB_UID]
    world.wo.user_event("dsync.user.deleted", BOB_UID, BOB_IDP)
    report = await world.tick()
    assert report is not None and report.removed == 1
    assert (await world.person(BOB_IDP))[2] == "deactivated"
    assert await world.live(sid) is None


async def test_a_full_sync_deactivates_people_the_directory_lost(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    await world.tick()
    del world.wo.users[BOB_UID]  # no event: missed, or older than the cursor
    assert (await world.tick()) == sync.TickReport(world.org)
    assert (await world.person(BOB_IDP))[2] == "active"
    await world.full_sync_due()
    report = await world.tick()
    assert report is not None and report.full and report.removed == 1
    assert (await world.person(BOB_IDP))[2] == "deactivated"


async def test_guests_are_not_members(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    await world.tick()
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com", raw_attributes={"userType": "guest"})
    world.wo.user_event("dsync.user.updated", BOB_UID)
    await world.tick()
    assert (await world.person(BOB_IDP))[2] == "deactivated"


async def test_directory_deletion_freezes_instead_of_deprovisioning(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    await world.tick()
    world.wo.event("dsync.deleted", {"id": world.wo.directory})
    report = await world.tick()
    assert report is not None and report.frozen
    assert (await world.person(BOB_IDP))[2] == "active"
    assert (await world.connection()).frozen
    assert await world.tick() is None
    frozen = await world.audit(AuditAction.DIRECTORY_FROZEN)
    assert [r[2] for r in frozen] == [{"state": "frozen", "reason": "directory_deleted"}]


async def test_an_empty_full_listing_freezes(world: World) -> None:
    await world.tick()
    world.wo.users.clear()
    await world.full_sync_due()
    report = await world.tick()
    assert report is not None and report.frozen
    assert (await world.person(FOUNDER_IDP))[2] == "active"


async def test_a_failed_fetch_is_recorded_and_retried(world: World) -> None:
    await world.tick()
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    world.wo.user_event("dsync.user.created", BOB_UID)
    world.wo.fail = 500
    report = await world.tick()
    assert report is not None and report.error is not None and "500" in report.error
    (row,) = await world.rows(
        "select last_error, event_cursor from ssc.directory_connection where org_id = :org"
    )
    assert row[0] is not None and row[1] is None
    world.wo.fail = None
    await world.tick()
    assert (await world.person(BOB_IDP))[2] == "active"
    (row,) = await world.rows(
        "select last_error, event_cursor from ssc.directory_connection where org_id = :org"
    )
    assert row == (None, "event_000001")


async def test_group_events_refresh_the_members(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    world.wo.group("directory_group_01OPS", "Ops", FOUNDER_UID)
    await world.tick()
    world.wo.members["directory_group_01OPS"].add(BOB_UID)
    world.wo.event(
        "dsync.group.user_added",
        {"directory_id": world.wo.directory, "group": {"id": "directory_group_01OPS"}},
    )
    await world.tick()
    members = await world.rows(
        "select count(*) from ssc.group_member m join ssc.user_group g "
        "on g.org_id = m.org_id and g.id = m.group_id "
        "where m.org_id = :org and g.directory_ref = 'directory_group_01OPS'"
    )
    assert members == [(2,)]
    del world.wo.groups["directory_group_01OPS"]
    world.wo.event(
        "dsync.group.deleted",
        {"directory_id": world.wo.directory, "id": "directory_group_01OPS"},
    )
    await world.tick()
    members = await world.rows("select count(*) from ssc.group_member where org_id = :org")
    assert members == [(0,)]


async def test_the_admin_group_sets_roles_but_never_removes_the_last_admin(dsns: Dsns) -> None:
    w = await new_world(dsns, admin_group_ref="directory_group_01ADMINS")
    try:
        w.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
        w.wo.group("directory_group_01ADMINS", "Admins", FOUNDER_UID, BOB_UID)
        await w.tick()
        assert (await w.person(BOB_IDP))[3] == "admin"
        w.wo.members["directory_group_01ADMINS"] = set()
        await w.full_sync_due()
        await w.tick()
        admins = await w.rows(
            "select count(*) from ssc.user_account where org_id = :org and role = 'admin' "
            "and status = 'active'"
        )
        assert admins == [(1,)]
    finally:
        await w.engine.dispose()


async def test_deactivation_reaches_the_snapshot(world: World) -> None:
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    await world.tick()
    bob = (await world.person(BOB_IDP))[0]
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com", state="inactive")
    world.wo.user_event("dsync.user.updated", BOB_UID)
    await world.tick()
    async with bound_org(world.engine, world.org) as conn:
        doc = await compile_document(conn, world.org, version=1, compiled_at=datetime.now(UTC))
    view = AccessView.from_document(doc)
    assert bob not in view.active_users and bob in view.not_before
    assert decide(view, "env_" + "x" * 20, bob).allowed is False


async def echo_app(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n")
    writer.write(b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\n\r\n")
    while chunk := await reader.read(1024):
        writer.write(chunk)
    writer.close()


async def test_removal_from_a_group_reaches_the_gateway_and_closes_the_open_stream(
    world: World, dsns: Dsns, tmp_path: Path
) -> None:
    fin = "directory_group_01FIN"
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    world.wo.group(fin, "Finance", BOB_UID)
    await world.tick()
    bob = (await world.person(BOB_IDP))[0]
    ((group,),) = await world.rows(
        "select id from ssc.user_group where org_id = :org and directory_ref = :ref", ref=fin
    )
    ((label,),) = await world.rows("select cell_label from ssc.org where id = :org")
    app, prod = new_id("app"), new_id("env")
    for sql in (
        "insert into ssc.app (id, org_id, slug, owner_user_id) values (:app, :org, 'ledger', :by)",
        "insert into ssc.environment (id, org_id, app_id, name) values (:env, :org, :app, 'prod')",
        "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, group_id, "
        "granted_by_user_id) values (:gnt, :org, :env, 'user', 'group', :grp, :by)",
    ):
        await world.rows(sql, app=app, env=prod, gnt=new_id("gnt"), grp=group, by=world.founder)
    signer = UrlSigner({"k1": secrets.token_bytes(32)}, active="k1", clock=SystemClock())
    blob = FsBlobStore(tmp_path, signer=signer, base_url="http://blobs.test/v1/blobs/")

    async def compile_and_point() -> None:
        async with bound_org(world.engine, world.org) as conn:
            await publish(conn, world.org, blob, at=datetime.now(UTC))
        await point_latest(world.engine, world.org, blob)

    await compile_and_point()
    clock = [1000.0]
    holder = ViewHolder(world.org)
    snap = OnDemandView(
        SnapshotFeed(blob, holder, monotonic=lambda: clock[0]), holder, max_stale=300
    )
    assert await snap.first_read()
    keyring = parse_keyring(new_keyring())
    config = GateConfig(
        org_id=world.org,
        cell_label=str(label),
        apps_domain="apps.test",
        auth_url="https://auth.example.test",
        issuer=f"https://keys.example.test/{label}",
        project_number="123456789012",
        region="us-central1",
        max_body_bytes=1024,
    )
    gate = gate_for(config, keyring, view=snap.view, refresh=snap.refresh)
    host, now = f"ledger.{label}.apps.test", int(time.time())
    who = Session(
        sid=new_sid(), sub=bob, org=world.org, name="Bob", email="bob@example.com",
        iat=now - 60, exp=now + 3600,
    )  # fmt: skip
    sealed = SessionCodec(keyring.session, active=keyring.session_kid).seal(who, host)
    ws = {"upgrade": "websocket", "connection": "upgrade", "origin": f"https://{host}"}
    facts = Facts(
        method="GET",
        host=host,
        path="/ws",
        headers={"cookie": f"__Host-ssc-session={sealed}", **ws},
    )
    allowed = await gate.check(facts)
    assert isinstance(allowed, Allow) and allowed.user == bob

    app_server = await asyncio.start_server(echo_app, "127.0.0.1", 0)
    app_port = app_server.sockets[0].getsockname()[1]

    async def dial(_: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", app_port)

    streams = Streams(lambda: gate, dial=dial, refresh=snap.refresh)
    relay = await streams.serve("127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", relay.sockets[0].getsockname()[1]
        )
        writer.write(
            f"GET /ws HTTP/1.1\r\nhost: {allowed.upstream}\r\n"
            f"{STREAM_HEADER}: {streams.admit(allowed)}\r\n\r\n".encode()
        )
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101 ")
        writer.write(b"ping")
        assert await reader.readexactly(4) == b"ping"

        with psycopg.connect(dsns.superuser) as conn:
            conn.execute("set search_path to procrastinate")
            conn.execute(
                "delete from procrastinate_jobs where queueing_lock = %s",
                (compile_lock(world.org),),
            )
        world.wo.members[fin].discard(BOB_UID)
        world.wo.event(
            "dsync.group.user_removed", {"directory_id": world.wo.directory, "group": {"id": fin}}
        )
        await world.tick()
        left = await world.rows(
            "select 1 from ssc.group_member where org_id = :org and user_id = :bob", bob=bob
        )
        assert left == []
        with psycopg.connect(dsns.superuser) as conn:
            queued = conn.execute(
                "select status from procrastinate.procrastinate_jobs where queueing_lock = %s",
                (compile_lock(world.org),),
            ).fetchall()
        assert queued == [("todo",)]
        await compile_and_point()
        clock[0] += RECHECK_SECONDS + 0.1
        started = time.monotonic()
        assert await asyncio.wait_for(reader.read(), WATCH_SECONDS + 2) == b""
        assert time.monotonic() - started <= WATCH_SECONDS + 0.5
        assert not streams.open
        refused = await gate.check(facts)
        assert isinstance(refused, Deny) and refused.reason == "not_granted"
    finally:
        await streams.aclose()
        relay.close()
        app_server.close()
        await snap.aclose()


# ── joining a login to a person ──────────────────────────────────────────────


async def test_an_okta_login_is_the_person_with_that_idp_id(world: World) -> None:
    await world.tick()
    found = await world.find(profile(world.wo, FOUNDER_IDP, "renamed@example.com"))
    assert found == join.Person(world.founder, active=True)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"organization_id": "org_01OTHER"}, "wrong_organization"),
        ({"connection_id": "conn_01OTHER"}, "connection_not_allowed"),
        ({"connection_type": "GoogleOAuth"}, "connection_type_refused"),
        ({"connection_type": "MicrosoftOAuth"}, "connection_type_refused"),
        ({"connection_type": "MagicLink"}, "connection_type_refused"),
        ({"connection_type": "GitHubOAuth"}, "connection_type_refused"),
    ],
)
async def test_a_login_from_elsewhere_is_refused(
    world: World, changes: dict[str, str], reason: str
) -> None:
    assert await world.find(profile(world.wo, FOUNDER_IDP, "ada@example.com", **changes)) == reason


async def test_a_login_with_no_usable_subject_is_refused_without_an_email_fallback(
    world: World,
) -> None:
    await world.tick()
    assert await world.find(profile(world.wo, "", "ada@example.com")) == "no_subject"
    assert (
        await world.find(profile(world.wo, "ada@example.com", "ada@example.com")) == "bad_subject"
    )


async def test_an_unmatched_login_waits_for_an_admin_link(world: World) -> None:
    await world.tick()
    world.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    world.wo.user_event("dsync.user.created", BOB_UID)
    await world.tick()
    bob = (await world.person(BOB_IDP))[0]
    stray = profile(world.wo, "00ustray", "bob@example.com")
    assert await world.find(stray) == "no_match"
    assert await world.find(stray) == "no_match"
    (row,) = await world.rows(
        "select id, subject, reason, attempts from ssc.unlinked_login where org_id = :org"
    )
    assert row[1:] == ("00ustray", "no_match", 2)
    async with bound_org(world.engine, world.org) as conn:
        with pytest.raises(join.LinkError, match="user_not_active"):
            await join.link_unlinked(conn, world.org, row[0], "usr_" + "z" * 20, actor=OPERATOR)
    async with bound_org(world.engine, world.org) as conn:
        await join.link_unlinked(conn, world.org, row[0], bob, actor=OPERATOR)
    assert await world.find(stray) == join.Person(bob, active=True)
    linked = await world.audit(AuditAction.IDENTITY_LINKED)
    assert [r[2]["user_id"] for r in linked] == [bob]
    async with bound_org(world.engine, world.org) as conn:
        with pytest.raises(join.LinkError, match="not_found"):
            await join.link_unlinked(conn, world.org, row[0], bob, actor=OPERATOR)


async def test_google_logins_join_by_the_current_holder_of_the_address(dsns: Dsns) -> None:
    w = await new_world(dsns, join_rule="email", founder_idp="110000000000000000001")
    try:
        w.wo.user(BOB_UID, "110000000000000000002", "sales@example.com")
        await w.tick()
        bob = (await w.person("110000000000000000002"))[0]
        saml = profile(w.wo, "sales@example.com", "Sales@Example.com", connection_type="GoogleSAML")
        assert await w.find(saml) == join.Person(bob, active=True)

        w.wo.user(BOB_UID, "110000000000000000002", "sales@example.com", state="inactive")
        w.wo.user("directory_user_01DAN", "110000000000000000003", "sales@example.com")
        w.wo.user_event("dsync.user.updated", BOB_UID)
        w.wo.user_event("dsync.user.created", "directory_user_01DAN")
        await w.tick()
        dan = (await w.person("110000000000000000003"))[0]
        assert await w.find(saml) == join.Person(dan, active=True)

        w.wo.user(BOB_UID, "110000000000000000002", "sales@example.com")
        w.wo.user_event("dsync.user.updated", BOB_UID)
        await w.tick()
        assert await w.find(saml) == "ambiguous_email"
        (unlinked,) = await w.rows("select id from ssc.unlinked_login where org_id = :org")
        async with bound_org(w.engine, w.org) as conn:
            with pytest.raises(join.LinkError, match="subject_not_keyable"):
                await join.link_unlinked(conn, w.org, unlinked[0], dan, actor=OPERATOR)
        assert await w.find(profile(w.wo, "x@example.com", "")) == "no_email"
    finally:
        await w.engine.dispose()


# ── sessions and login codes ─────────────────────────────────────────────────


async def code_for(w: World, sid: str, host: str = "ledger.cell.apps.test") -> tuple[str, str]:
    nonce = secrets.token_urlsafe(32)
    async with bound_org(w.engine, w.org) as conn:
        code = await sessions.issue_code(
            conn, w.org, session_id=sid, host=host, binding_hash=sessions.digest(nonce)
        )
    return code, nonce


async def redeem(w: World, code: str, nonce: str, host: str = "ledger.cell.apps.test") -> Any:
    async with bound_org(w.engine, w.org) as conn:
        return await sessions.redeem_code(conn, w.org, code=code, host=host, nonce=nonce)


async def test_a_code_works_once_for_its_host_and_browser(world: World) -> None:
    sid = await world.open(world.founder)
    code, nonce = await code_for(world, sid)
    done = await redeem(world, code, nonce)
    assert done is not None and done.user_id == world.founder
    assert 0 < done.exp - done.iat <= sessions.SESSION_SECONDS
    assert await redeem(world, code, nonce) is None
    for wrong in ({"host": "payroll.cell.apps.test"}, {"nonce": "another-browser"}):
        code, nonce = await code_for(world, sid)
        args = {"code": code, "nonce": nonce, **wrong}
        assert await redeem(world, **args) is None
        assert await redeem(world, code, nonce) is None, "a wrong try uses the code up"


async def test_an_old_code_or_a_dead_session_signs_no_one_in(world: World) -> None:
    sid = await world.open(world.founder)
    code, nonce = await code_for(world, sid)
    await world.rows("update ssc.login_code set expires_at = now() where org_id = :org")
    assert await redeem(world, code, nonce) is None
    code, nonce = await code_for(world, sid)
    async with bound_org(world.engine, world.org) as conn:
        await sessions.revoke_session(conn, world.org, sid, "logout", actor=OPERATOR)
    assert await redeem(world, code, nonce) is None


async def test_a_session_lives_twelve_hours_and_is_never_extended(world: World) -> None:
    sid = await world.open(world.founder)
    live = await world.live(sid)
    assert live is not None
    assert (live.expires_at - live.created_at).total_seconds() == sessions.SESSION_SECONDS
    await world.rows(
        "update ssc.auth_session set created_at = now() - interval '12 hours 1 second', "
        "expires_at = now() - interval '1 second' where org_id = :org and id = :id",
        id=sid,
    )
    assert await world.live(sid) is None


# ── command-line tokens ──────────────────────────────────────────────────────


async def test_the_device_flow_yields_one_session(world: World) -> None:
    async with bound_org(world.engine, world.org) as conn:
        start = await tokens.start_device(conn, world.org)
    assert len(start.user_code) == tokens.USER_CODE_LENGTH
    assert tokens.org_of(start.device_code) is not None

    async def poll() -> str:
        async with bound_org(world.engine, world.org) as conn:
            return await tokens.poll_device(conn, world.org, start.device_code)

    assert await poll() == "authorization_pending"
    assert await poll() == "slow_down"
    pretty = f"{start.user_code[:4]}-{start.user_code[4:].lower()}"
    sid = await world.open(world.founder, "cli")
    async with bound_org(world.engine, world.org) as conn:
        grant = await tokens.pending_grant(conn, world.org, pretty)
        assert grant is not None
        assert await tokens.decide_grant(conn, world.org, grant, session_id=sid)
        assert not await tokens.decide_grant(conn, world.org, grant, session_id=None)
    assert await poll() == sid
    assert await poll() == "expired_token"
    async with bound_org(world.engine, world.org) as conn:
        assert await tokens.poll_device(conn, world.org, f"{world.org}.nope") == "expired_token"


async def test_a_denied_or_expired_grant(world: World) -> None:
    async with bound_org(world.engine, world.org) as conn:
        denied, expired = (
            await tokens.start_device(conn, world.org),
            await tokens.start_device(conn, world.org),
        )
        grant = await tokens.pending_grant(conn, world.org, denied.user_code)
        assert grant is not None
        await tokens.decide_grant(conn, world.org, grant, session_id=None)
    await world.rows(
        "update ssc.device_grant set expires_at = now() where org_id = :org and user_code = :c",
        c=expired.user_code,
    )
    async with bound_org(world.engine, world.org) as conn:
        assert await tokens.poll_device(conn, world.org, denied.device_code) == "access_denied"
        assert await tokens.poll_device(conn, world.org, expired.device_code) == "expired_token"
        assert await tokens.pending_grant(conn, world.org, expired.user_code) is None


async def test_refresh_tokens_rotate_and_a_reused_one_ends_the_session(world: World) -> None:
    sid = await world.open(world.founder, "cli")
    async with bound_org(world.engine, world.org) as conn:
        first = await tokens.issue_refresh(conn, world.org, sid)
        second = await tokens.rotate_refresh(conn, world.org, first, actor=OPERATOR)
    assert second is not None and second.session_id == sid and second.refresh_token != first
    assert tokens.org_of(second.refresh_token, tokens.REFRESH_PREFIX) is not None
    async with bound_org(world.engine, world.org) as conn:
        assert await tokens.rotate_refresh(conn, world.org, first, actor=OPERATOR) is None
    assert await world.live(sid) is None
    async with bound_org(world.engine, world.org) as conn:
        rotated = await tokens.rotate_refresh(conn, world.org, second.refresh_token, actor=OPERATOR)
        assert rotated is None
    revoked = await world.audit(AuditAction.TOKEN_REVOKED)
    assert [r[2] for r in revoked] == [{"reason": "refresh_reuse"}]


async def test_a_browser_session_cannot_mint_refresh_tokens(world: World) -> None:
    sid = await world.open(world.founder, "browser")
    async with bound_org(world.engine, world.org) as conn:
        raw = await tokens.issue_refresh(conn, world.org, sid)
        assert await tokens.rotate_refresh(conn, world.org, raw, actor=OPERATOR) is None


def test_token_shapes() -> None:
    org = "org_" + "a" * 20
    assert tokens.org_of(f"{org}.s3cret") == (org, "s3cret")
    assert tokens.org_of(f"ssc_rt.{org}.s3cret", tokens.REFRESH_PREFIX) == (org, "s3cret")
    for bad in ("", org, f"{org}.", "org_bad.s", f"ssc_rt.{org}.s", f"x.{org}.s.t"):
        assert tokens.org_of(bad) is None
    assert tokens.org_of(f"{org}.s", tokens.REFRESH_PREFIX) is None
    assert set(tokens.new_user_code()) <= set(tokens.USER_CODE_ALPHABET)
    assert tokens.normal_user_code("bcdf-ghjk") == "BCDFGHJK"
