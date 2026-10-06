"""SSC-097: org creation checked against WorkOS, and the audited recovery of an org's admin,
against a real postgres:18 and a fake WorkOS (decision 024).

Ticket "done when" checks:
  * create-org refuses an admin keyed under an idp_id WorkOS does not have and writes nothing; it
        passes with the right one ->
        test_create_org_refuses_a_wrongly_keyed_founder_and_writes_nothing and
        test_create_org_with_the_right_founder_writes_three_events
  * restore-admin restores a deactivated admin, and the audit chain verifies and shows both
        events -> test_restore_admin_reactivates_the_founder_and_the_chain_verifies
  * steps 10.2 and 10.3 of the runbook hold no SQL: docs only
"""

import argparse
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from fake_workos import FakeWorkOS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns
from test_identity import BOB_IDP, BOB_UID, FOUNDER_IDP, FOUNDER_UID, World, new_world

from ssc_contracts.audit import ActorKind
from ssc_control.audit.chain import Actor
from ssc_control.audit.verify import verify
from ssc_control.db import NewOrg, bound_org, create_org, make_engine
from ssc_control.identity import __main__ as cli
from ssc_control.identity import connections, operator, sync
from ssc_control.identity.connections import ConnectError
from ssc_control.identity.rules import JoinRule

OP = Actor(kind=ActorKind.OPERATOR, id="op_dana")
LABEL = "qwrtplkjhgfd"
DIRECTORY_USER = "directory_user_01ADA"
EMAIL = "ada@example.com"


@pytest.fixture
async def engine(dsns: Dsns) -> AsyncIterator[AsyncEngine]:
    e = make_engine(dsns.app)
    try:
        yield e
    finally:
        await e.dispose()


def directory_with_founder(idp_id: str = FOUNDER_IDP, email: str = EMAIL, **kw: Any) -> FakeWorkOS:
    wo = FakeWorkOS()
    wo.user(DIRECTORY_USER, idp_id, email, first="Ada", last="Admin", **kw)
    return wo


async def create(
    engine: AsyncEngine,
    wo: FakeWorkOS,
    *,
    idp_id: str = FOUNDER_IDP,
    join_rule: JoinRule = "idp_id",
    label: str | None = LABEL,
    name: str = "Acme",
    admin_group_ref: str | None = None,
) -> operator.OrgSetUp:
    client = wo.client()
    try:
        return await operator.create_org_with_directory(
            engine,
            client,
            name=name,
            founder_name="Ada Admin",
            founder_email=EMAIL,
            founder_idp_id=idp_id,
            workos_organization_id=wo.organization,
            workos_directory_id=wo.directory,
            sso_connection_ids=[wo.sso],
            join_rule=join_rule,
            admin_group_ref=admin_group_ref,
            cell_label=label,
            actor=OP,
        )
    finally:
        await client.aclose()


def counts(dsns: Dsns, name: str, directory: str) -> tuple[int, int, int]:
    """Orgs of that name, connections to that directory, and every org's audit rows: read as the
    superuser, which row-level security does not hide anything from."""
    with psycopg.connect(dsns.superuser) as conn:
        orgs = conn.execute("select count(*) from ssc.org where name = %s", (name,)).fetchone()
        links = conn.execute(
            "select count(*) from ssc.directory_connection where workos_directory_id = %s",
            (directory,),
        ).fetchone()
        events = conn.execute(
            "select count(*) from ssc.audit_event e join ssc.org o on o.id = e.org_id "
            "where o.name = %s",
            (name,),
        ).fetchone()
    assert orgs is not None
    assert links is not None
    assert events is not None
    return int(orgs[0]), int(links[0]), int(events[0])


async def events_of(engine: AsyncEngine, org: str) -> list[Any]:
    async with bound_org(engine, org) as conn:
        result = await conn.execute(
            text(
                "select seq, action, actor_kind, actor_id, target_kind, target_id, before, after "
                "from ssc.audit_event where org_id = :org order by seq"
            ),
            {"org": org},
        )
        return list(result.all())


async def chain_ok(engine: AsyncEngine, org: str) -> int:
    """The chain verifies; its head seq."""
    async with bound_org(engine, org) as conn:
        report = await verify(conn, org)
    assert report.ok, report
    assert report.head_seq is not None
    return report.head_seq


# ── create-org ───────────────────────────────────────────────────────────────


async def test_create_org_refuses_a_wrongly_keyed_founder_and_writes_nothing(
    dsns: Dsns, engine: AsyncEngine
) -> None:
    wo = directory_with_founder()
    before_all = counts(dsns, "Acme-wrong", wo.directory)
    with pytest.raises(ConnectError) as refused:
        await create(engine, wo, idp_id="0000-not-in-workos", name="Acme-wrong")
    message = str(refused.value)
    assert "no active directory user has idp_id 0000-not-in-workos" in message
    assert f"{DIRECTORY_USER} (idp_id {FOUNDER_IDP}, active)" in message
    assert counts(dsns, "Acme-wrong", wo.directory) == before_all == (0, 0, 0)
    with psycopg.connect(dsns.superuser) as conn:
        taken = conn.execute("select 1 from ssc.org where cell_label = %s", (LABEL,)).fetchall()
    assert taken == []


async def test_create_org_with_the_right_founder_writes_three_events(
    dsns: Dsns, engine: AsyncEngine
) -> None:
    wo = directory_with_founder()
    made = await create(engine, wo, label="plmoknijbuhv", name="Acme-right")
    org = made.org
    assert made.founder[0].ok
    assert org.cell_label != "plmoknijbuhv"  # the generated one, replaced
    rows = await events_of(engine, org.org_id)
    assert [r[1] for r in rows] == ["org.created", "directory.connected", "org.updated"]
    assert {(r[2], r[3]) for r in rows} == {("operator", "op_dana")}
    assert rows[2][4:8] == (
        "org",
        org.org_id,
        {"cell_label": org.cell_label},
        {"cell_label": "plmoknijbuhv"},
    )
    assert await chain_ok(engine, org.org_id) == 3
    async with bound_org(engine, org.org_id) as conn:
        connection = await connections.load(conn, org.org_id)
        (label,) = (await conn.execute(text("select cell_label from ssc.org"))).one()
    assert connection is not None
    assert connection.id == made.connection_id
    assert connection.workos_directory_id == wo.directory
    assert label == "plmoknijbuhv"
    # the first sync, over the same directory, keeps the founder active and the only admin
    client = wo.client()
    try:
        wo.user(BOB_UID, BOB_IDP, "bob@example.com")
        report = await sync.tick(engine, client, org.org_id)
    finally:
        await client.aclose()
    assert report is not None
    assert report.error is None
    async with bound_org(engine, org.org_id) as conn:
        status = (
            await conn.execute(
                text("select status, role from ssc.user_account where id = :id"),
                {"id": org.admin_user_id},
            )
        ).one()
    assert tuple(status) == ("active", "admin")


async def test_create_org_without_a_cell_label_keeps_the_generated_one(
    dsns: Dsns, engine: AsyncEngine
) -> None:
    wo = directory_with_founder()
    made = await create(engine, wo, label=None, name="Acme-nolabel")
    rows = await events_of(engine, made.org.org_id)
    assert [r[1] for r in rows] == ["org.created", "directory.connected"]
    async with bound_org(engine, made.org.org_id) as conn:
        (label,) = (await conn.execute(text("select cell_label from ssc.org"))).one()
    assert label == made.org.cell_label
    assert await chain_ok(engine, made.org.org_id) == 2


async def test_the_founder_check_under_the_email_rule(dsns: Dsns, engine: AsyncEngine) -> None:
    wo = directory_with_founder(idp_id="108123456789")
    made = await create(
        engine, wo, idp_id="108123456789", join_rule="email", label="bcdfghjklmnp", name="G-ok"
    )
    assert made.founder[0].ok
    # the address names one active user but the linked subject is not that user's idp_id
    other = directory_with_founder(idp_id="108123456789")
    with pytest.raises(ConnectError, match="whose idp_id is 108123456789, not the linked subject"):
        await create(engine, other, idp_id=EMAIL, join_rule="email", name="G-subject")
    # two active users hold the address
    two = directory_with_founder(idp_id="1081")
    two.user("directory_user_01TWO", "1082", EMAIL)
    with pytest.raises(ConnectError, match="2 active directory users have this admin's email"):
        await create(engine, two, idp_id="1081", join_rule="email", name="G-two")
    # no active user holds it: the only one is suspended
    gone = directory_with_founder(idp_id="1081", state="suspended")
    with pytest.raises(ConnectError, match="no active directory user has this admin's email") as e:
        await create(engine, gone, idp_id="1081", join_rule="email", name="G-none")
    assert f"{DIRECTORY_USER} (idp_id 1081, not active)" in str(e.value)
    for name, wo_ in (("G-subject", other), ("G-two", two), ("G-none", gone)):
        assert counts(dsns, name, wo_.directory) == (0, 0, 0)


async def test_an_inactive_or_guest_directory_user_does_not_pass(
    dsns: Dsns, engine: AsyncEngine
) -> None:
    inactive = directory_with_founder(state="suspended")
    with pytest.raises(ConnectError, match="no active directory user has idp_id"):
        await create(engine, inactive, name="Idle")
    guest = directory_with_founder(custom_attributes={"userType": "Guest"})
    with pytest.raises(ConnectError, match="no active directory user has idp_id"):
        await create(engine, guest, name="Guest")
    assert counts(dsns, "Idle", inactive.directory) == (0, 0, 0)
    assert counts(dsns, "Guest", guest.directory) == (0, 0, 0)


async def test_a_cell_label_another_org_has_rolls_everything_back(
    dsns: Dsns, engine: AsyncEngine
) -> None:
    first = await create(engine, directory_with_founder(), label="zxcvbnmasdfg", name="First")
    wo = directory_with_founder()
    with pytest.raises(ConnectError, match="already used by another org"):
        await create(engine, wo, label="zxcvbnmasdfg", name="Second")
    assert counts(dsns, "Second", wo.directory) == (0, 0, 0)
    assert await chain_ok(engine, first.org.org_id) == 3


async def test_a_failed_workos_read_writes_nothing(dsns: Dsns, engine: AsyncEngine) -> None:
    wo = directory_with_founder()
    wo.fail = 500
    with pytest.raises(Exception, match="HTTP 500"):
        await create(engine, wo, name="Down")
    assert counts(dsns, "Down", wo.directory) == (0, 0, 0)


async def test_connect_checks_the_founder_in_workos_too(dsns: Dsns) -> None:
    w = await new_world(dsns)  # keyed FOUNDER_IDP, already connected
    try:
        # a second directory the founder is not keyed under: the database check refuses first
        elsewhere = FakeWorkOS()
        with pytest.raises(ConnectError, match="no active admin is linked"):
            await cli.run_connect(
                dsns.app, elsewhere.client(), _connect_args(w.org, elsewhere, "idp_id")
            )
        # the same directory as the founder's, but WorkOS now has them under another idp_id
        wo = FakeWorkOS(directory=w.wo.directory)
        wo.user(FOUNDER_UID, "00uchanged", "ada@example.com")
        same = _connect_args(w.org, wo, "idp_id")
        with pytest.raises(ConnectError, match="no active directory user has idp_id 00ufounder"):
            await cli.run_connect(dsns.app, wo.client(), same)
        wo.user(FOUNDER_UID, FOUNDER_IDP, "ada@example.com")
        assert (await cli.run_connect(dsns.app, wo.client(), same)).startswith("dcn_")
    finally:
        await w.engine.dispose()


def _connect_args(org: str, wo: FakeWorkOS, rule: str) -> argparse.Namespace:
    return argparse.Namespace(
        org=org,
        operator="op_dana",
        workos_org=wo.organization,
        directory=wo.directory,
        sso=[wo.sso],
        join_rule=rule,
        admin_group=None,
    )


# ── restore-admin ────────────────────────────────────────────────────────────


async def deactivated_founder(dsns: Dsns, **kw: Any) -> World:
    """The 2026-10-06 state: the first full sync could not find the founder and deactivated
    them, and the org has no active admin."""
    w = await new_world(dsns, **kw)
    w.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
    del w.wo.users[FOUNDER_UID]
    report = await w.tick()
    assert report is not None
    assert report.error is None
    assert (await w.person(FOUNDER_IDP))[2:] == ("deactivated", "admin")
    return w


async def restore(w: World, user: str, **kw: Any) -> operator.Restored:
    async with bound_org(w.engine, w.org) as conn:
        return await operator.restore_admin(
            conn, w.org, user, actor=OP, reason=kw.pop("reason", "founder locked out"), **kw
        )


async def test_restore_admin_reactivates_the_founder_and_the_chain_verifies(dsns: Dsns) -> None:
    w = await deactivated_founder(dsns)
    try:
        before = await events_of(w.engine, w.org)
        done = await restore(w, w.founder)
        assert (done.role_before, done.status_before, done.recorded_only) == (
            "admin",
            "deactivated",
            False,
        )
        assert (await w.person(FOUNDER_IDP))[2:] == ("active", "admin")
        assert await w.rows(
            "select deactivated_at from ssc.user_account where id = :id", id=w.founder
        ) == [(None,)]
        new = (await events_of(w.engine, w.org))[len(before) :]
        assert [(r[1], r[2], r[3], r[4], r[5]) for r in new] == [
            ("user.updated", "operator", "op_dana", "user_account", w.founder),
            ("user.reactivated", "operator", "op_dana", "user_account", w.founder),
            ("operator.access", "operator", "op_dana", "user_account", w.founder),
        ]
        assert new[0][6:8] == (
            {"role": "admin", "status": "deactivated"},
            {"role": "admin", "status": "active"},
        )
        assert new[1][6:8] == ({"status": "deactivated"}, {"status": "active"})
        assert new[2][7] == {"reason": "founder locked out"}
        assert await chain_ok(w.engine, w.org) == len(before) + 3
        # the next full sync keeps them: they are in the directory again
        w.wo.user(FOUNDER_UID, FOUNDER_IDP, "ada@example.com")
        await w.full_sync_due()
        await w.tick()
        assert (await w.person(FOUNDER_IDP))[2:] == ("active", "admin")
    finally:
        await w.engine.dispose()


async def test_restore_admin_promotes_a_member_and_writes_no_reactivation(dsns: Dsns) -> None:
    w = await new_world(dsns)
    try:
        w.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
        await w.tick()
        bob = (await w.person(BOB_IDP))[0]
        assert (await w.person(BOB_IDP))[2:] == ("active", "member")
        done = await restore(w, bob)
        assert (done.role_before, done.status_before) == ("member", "active")
        actions = [r[1] for r in await events_of(w.engine, w.org)]
        assert actions[-2:] == ["user.updated", "operator.access"]
        await chain_ok(w.engine, w.org)
    finally:
        await w.engine.dispose()


async def test_restore_admin_refusals_write_nothing(dsns: Dsns) -> None:
    w = await deactivated_founder(dsns)
    other = await new_world(dsns)
    try:
        before = await events_of(w.engine, w.org)

        async def refused(user: str, match: str, org: World = w, **kw: Any) -> None:
            with pytest.raises(operator.RestoreError, match=match):
                await restore(org, user, **kw)

        await refused(other.founder, "is not a user of")  # another org's user, from this org
        await refused("usr_" + "z" * 20, "is not a user of")
        for bad in ("", "x" * 201, "mail ada@example.com", "ticket 123456", "two\nlines"):
            await refused(w.founder, "reason", reason=bad)
        # an active admin has nothing to restore
        await restore(w, w.founder)
        await refused(w.founder, "already an active admin")
        assert len(await events_of(w.engine, w.org)) == len(before) + 3
    finally:
        await w.engine.dispose()
        await other.engine.dispose()


async def test_restore_admin_needs_a_link_under_the_directory_and_a_connection(
    dsns: Dsns,
) -> None:
    w = await new_world(dsns)
    try:
        # a person the directory never made: linked only under another issuer
        async with bound_org(w.engine, w.org) as conn:
            await conn.execute(
                text(
                    "insert into ssc.user_account (id, org_id, display_name, email, role) "
                    "values ('usr_"
                    + "m" * 20
                    + "', :org, 'Manual', 'manual@example.com', 'member')"
                ),
                {"org": w.org},
            )
            await conn.execute(
                text(
                    "insert into ssc.identity_link (id, org_id, user_id, issuer, subject) "
                    "values ('idl_" + "m" * 20 + "', :org, 'usr_" + "m" * 20 + "', "
                    "'https://elsewhere.test', 'x')"
                ),
                {"org": w.org},
            )
        with pytest.raises(operator.RestoreError, match="no identity link under the org's"):
            await restore(w, "usr_" + "m" * 20)
    finally:
        await w.engine.dispose()
    # an org with no connection at all
    engine = make_engine(dsns.app)
    try:
        bare = await create_org(
            engine, NewOrg("Bare", "Bo", "bo@example.com", "https://x.test", "s")
        )
        async with bound_org(engine, bare.org_id) as conn:
            with pytest.raises(operator.RestoreError, match="no directory connection"):
                await operator.restore_admin(
                    conn, bare.org_id, bare.admin_user_id, actor=OP, reason="r"
                )
    finally:
        await engine.dispose()


async def test_the_admin_group_guard_and_its_override(dsns: Dsns) -> None:
    group = "directory_group_01ADMINS"
    w = await new_world(dsns, admin_group_ref=group)
    try:
        w.wo.user(BOB_UID, BOB_IDP, "bob@example.com")
        w.wo.group(group, "Admins", FOUNDER_UID)  # Bob is not in the admin group
        await w.tick()
        bob = (await w.person(BOB_IDP))[0]
        with pytest.raises(operator.RestoreError, match="not in the org's admin group"):
            await restore(w, bob)
        assert (await w.person(BOB_IDP))[3] == "member"
        done = await restore(w, bob, outside_admin_group=True)
        assert len(done.warnings) == 1
        assert "sync will demote them once another admin exists" in done.warnings[0]
        assert (await w.person(BOB_IDP))[3] == "admin"
        # a member of the admin group needs no flag
        w.wo.user("directory_user_01CAT", "00ucat", "cat@example.com")
        w.wo.members[group].add("directory_user_01CAT")
        await w.full_sync_due()
        await w.tick()
        cat = (await w.person("00ucat"))[0]
        await w.rows("update ssc.user_account set role = 'member' where id = :id", id=cat)
        done = await restore(w, cat)
        assert done.warnings == ()
        await chain_ok(w.engine, w.org)
    finally:
        await w.engine.dispose()


async def test_an_already_applied_change_is_recorded_once_without_touching_the_row(
    dsns: Dsns,
) -> None:
    w = await deactivated_founder(dsns)
    try:
        applied = datetime(2026, 10, 6, 4, 25, tzinfo=UTC)
        # the row is not an active admin yet: there is nothing applied to record
        with pytest.raises(operator.RestoreError, match="not an active admin now"):
            await restore(w, w.founder, already_applied_at=applied)
        await w.rows(  # the founder's own SQL update of that night
            "update ssc.user_account set status = 'active', deactivated_at = null where id = :id",
            id=w.founder,
        )
        row_before = await w.rows("select * from ssc.user_account where id = :id", id=w.founder)
        before = await events_of(w.engine, w.org)
        done = await restore(
            w,
            w.founder,
            reason="founder repair by SQL, org had no admin",
            already_applied_at=datetime(2026, 10, 6, 6, 25, tzinfo=timezone(timedelta(hours=2))),
        )
        assert done.recorded_only
        assert (
            await w.rows("select * from ssc.user_account where id = :id", id=w.founder)
            == row_before
        )
        new = (await events_of(w.engine, w.org))[len(before) :]
        assert len(new) == 1
        assert new[0][1:6] == ("operator.access", "operator", "op_dana", "user_account", w.founder)
        assert new[0][7] == {
            "role": "admin",
            "status": "active",
            "reason": "founder repair by SQL, org had no admin",
            "applied_at": applied.isoformat(),
            "applied_via": "sql",
        }
        assert await chain_ok(w.engine, w.org) == len(before) + 1
        # the same change twice, no offset, and the future are all refused
        with pytest.raises(operator.RestoreError, match="already recorded"):
            await restore(w, w.founder, already_applied_at=applied)
        with pytest.raises(operator.RestoreError, match="UTC offset"):
            await restore(w, w.founder, already_applied_at=datetime(2026, 10, 6, 4, 26))  # noqa: DTZ001
        with pytest.raises(operator.RestoreError, match="in the future"):
            await restore(w, w.founder, already_applied_at=datetime.now(UTC) + timedelta(hours=1))
        assert len(await events_of(w.engine, w.org)) == len(before) + 1
    finally:
        await w.engine.dispose()


# ── the command line ─────────────────────────────────────────────────────────


def test_the_commands_validate_their_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli._parser()  # noqa: SLF001
    ok = parser.parse_args(
        [
            "restore-admin",
            "--org",
            "org_" + "a" * 20,
            "--user",
            "usr_" + "b" * 20,
            "--operator",
            "op_dana",
            "--reason",
            " founder locked out ",
            "--already-applied-at",
            "2026-10-06T04:25:00Z",
        ]
    )
    assert ok.reason == "founder locked out"
    assert ok.already_applied_at == datetime(2026, 10, 6, 4, 25, tzinfo=UTC)
    base = ["--org", "org_" + "a" * 20, "--user", "usr_" + "b" * 20, "--reason", "r"]
    for bad in (["--operator", "dana"], ["--operator", "op_Dana"]):
        with pytest.raises(SystemExit):
            parser.parse_args(["restore-admin", *base, *bad])
    for label in ("short", "Upper12345678", "1abcdefghijk", "a" * 17):
        with pytest.raises(SystemExit):
            parser.parse_args(_create_argv(label))
    assert parser.parse_args(_create_argv(LABEL)).cell_label == LABEL
    assert parser.parse_args(_create_argv(None)).cell_label is None
    capsys.readouterr()


def _create_argv(label: str | None) -> list[str]:
    return [
        "create-org",
        "--name",
        "Acme",
        "--founder-name",
        "Ada",
        "--founder-email",
        EMAIL,
        "--founder-idp-id",
        FOUNDER_IDP,
        "--operator",
        "op_dana",
        "--workos-org",
        "org_01X",
        "--directory",
        "directory_01X",
        "--sso",
        "conn_01X",
        "--join-rule",
        "idp_id",
        *([] if label is None else ["--cell-label", label]),
    ]


def test_a_command_that_needs_workos_says_so_when_the_key_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("SSC_WORKOS_API_KEY", "SSC_WORKOS_CLIENT_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SSC_DATABASE_DSN", "postgresql://nobody@127.0.0.1:1/none")
    assert cli.main(_create_argv(LABEL)) == 2
    assert "SSC_WORKOS_API_KEY" in capsys.readouterr().err
    assert os.environ.get("SSC_WORKOS_API_KEY") is None


async def test_run_create_org_prints_the_org_and_the_founder_result(
    dsns: Dsns, capsys: pytest.CaptureFixture[str]
) -> None:
    wo = directory_with_founder()
    args = cli._parser().parse_args(_create_argv("hjklqwertyui"))  # noqa: SLF001
    args.workos_org, args.directory, args.sso = wo.organization, wo.directory, [wo.sso]
    org_id = await cli.run_create_org(dsns.app, wo.client(), args)
    assert org_id.startswith("org_")
    err = capsys.readouterr().err
    assert "usr_" in err
    assert "(subject 00ufounder): ok" in err
