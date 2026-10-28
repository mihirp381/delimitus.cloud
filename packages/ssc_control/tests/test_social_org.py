"""An org created directly in social mode (plan A7, decision 002 amendment 2026-10-27), against
postgres:18 and a fake WorkOS.

  1 create-social-org: one transaction, four events, no WorkOS -> test_create_social_org_makes_...,
                                                                  test_create_social_org_refuses_...
  2 the admin signs in and invites; the member signs in       -> test_the_invited_admin_signs_in_...
  3 restore-admin needs a social link in a social org          -> test_restore_admin_in_a_social_...
  5 the chooser lists its providers; a miss is the same miss   -> test_the_chooser_of_a_created_...
  6 the directory path is unchanged                            -> every existing suite
"""

import secrets
from typing import Any

import psycopg
import pytest
from fake_workos import FakeWorkOS
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns, assert_problem
from test_auth_host import AUTH, run
from test_identity import World, profile
from test_oauth import hidden
from test_org_admin import OP, chain_ok, events_of, restore
from test_social_signin import BOB_SUB, GOOGLE_SUB, Api, invite, rig_on, signed_in, social_login

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.db import bound_org, make_engine
from ssc_control.identity import __main__ as cli
from ssc_control.identity import operator, pages, social
from ssc_control.identity.connections import ConnectError

WORKOS_ENV = ("SSC_WORKOS_API_KEY", "SSC_WORKOS_CLIENT_ID", "SSC_WORKOS_BASE")


def fresh_label() -> str:
    return "s" + secrets.token_hex(6)


def argv(**change: str | None) -> list[str]:
    args: dict[str, str | None] = {
        "--name": "Pilot",
        "--admin-email": "ada@example.com",
        "--providers": "google,microsoft",
        "--cell-label": fresh_label(),
        "--operator": "op_dana",
    }
    args.update({f"--{k.replace('_', '-')}": v for k, v in change.items()})
    return ["create-social-org", *(p for k, v in args.items() if v is not None for p in (k, v))]


def without_workos(dsns: Dsns, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in WORKOS_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)


def org_rows(dsns: Dsns, name: str) -> tuple[int, int]:
    with psycopg.connect(dsns.superuser) as conn:
        row = conn.execute(
            "select count(distinct o.id), count(e.seq) from ssc.org o "
            "left join ssc.audit_event e on e.org_id = o.id where o.name = %s",
            (name,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


async def social_org(engine: AsyncEngine, *providers: str) -> World:
    made = await operator.create_social_org(
        engine,
        name="Pilot",
        admin_email="Ada@Example.com",
        providers=providers or ("google",),
        cell_label=fresh_label(),
        actor=OP,
    )
    return World(engine, made.org_id, made.admin_user_id, FakeWorkOS())


async def who(w: World, user: str) -> tuple[str, str, str]:
    (row,) = await w.rows(
        "select role, status, display_name from ssc.user_account where id = :id", id=user
    )
    return str(row[0]), str(row[1]), str(row[2])


async def session_of(w: World, user: str) -> str:
    (row,) = await w.rows(
        "select id from ssc.auth_session where org_id = :org and user_id = :id", id=user
    )
    return str(row[0])


# ── the command ──────────────────────────────────────────────────────────────


def test_create_social_org_makes_the_org_its_invited_admin_and_four_events(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    without_workos(dsns, monkeypatch)
    label, name = fresh_label(), f"Pilot-{secrets.token_hex(4)}"
    assert cli.main(argv(name=name, admin_email=" Ada@Example.COM ", cell_label=label)) == 0
    org = capsys.readouterr().out.strip()
    assert org.startswith("org_")
    with psycopg.connect(dsns.superuser) as conn:
        events = conn.execute(
            "select action, actor_kind, actor_id, target_kind, target_id, before, after "
            "from ssc.audit_event where org_id = %s order by seq",
            (org,),
        ).fetchall()
        people = conn.execute(
            "select id, role, status, email, display_name from ssc.user_account where org_id = %s",
            (org,),
        ).fetchall()
        state = conn.execute(
            "select name, sign_in_mode, social_providers, cell_label, "
            "(select count(*) from ssc.identity_link where org_id = o.id), "
            "(select count(*) from ssc.directory_connection where org_id = o.id), "
            "(select count(*) from ssc.org_index where org_id = o.id) "
            "from ssc.org o where id = %s",
            (org,),
        ).fetchone()
    ((admin, *person),) = people
    assert tuple(person) == ("admin", "invited", "ada@example.com", "ada@example.com")
    assert state == (name, "social", ["google", "microsoft"], label, 0, 0, 1)
    assert [e[0] for e in events] == ["org.created", "user.invited", "org.updated", "org.updated"]
    assert {(e[1], e[2]) for e in events} == {("operator", "op_dana")}
    assert events[1][3:] == ("user_account", admin, None, {"role": "admin", "status": "invited"})
    assert events[2][6] == {"sign_in_mode": "social", "social_providers": ["google", "microsoft"]}
    assert events[3][6] == {"cell_label": label}

    async def verified(engine: AsyncEngine) -> int:
        return await chain_ok(engine, org)

    assert run(dsns, verified) == 4


def test_create_social_org_refuses_bad_input_before_writing(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    without_workos(dsns, monkeypatch)
    name = f"Refused-{secrets.token_hex(4)}"
    for bad in (
        {"providers": "facebook"},
        {"providers": ","},
        {"providers": None},
        {"cell_label": None},
        {"cell_label": "Short"},
        {"admin_email": "not-an-address"},
        {"admin_email": "a@b@example.com"},
        {"admin_email": "two words@example.com"},
        {"admin_email": None},
        {"operator": "dana"},
    ):
        with pytest.raises(SystemExit) as refused:
            cli.main(argv(name=name, **bad))
        assert refused.value.code == 2, bad
    capsys.readouterr()
    assert org_rows(dsns, name) == (0, 0)
    taken = fresh_label()
    assert cli.main(argv(name=f"{name}-first", cell_label=taken)) == 0
    assert cli.main(argv(name=name, cell_label=taken)) == 1
    assert "not created: the cell label or the org id" in capsys.readouterr().err
    assert org_rows(dsns, name) == (0, 0)
    assert org_rows(dsns, f"{name}-first") == (1, 4)


async def test_create_social_org_refuses_a_malformed_address_or_label(dsns: Dsns) -> None:
    engine = make_engine(dsns.app)
    try:
        for change in ({"admin_email": "nobody"}, {"cell_label": "UPPERCASE1"}):
            kw: dict[str, Any] = {
                "name": "Bad-direct",
                "admin_email": "ada@example.com",
                "providers": ("google",),
                "cell_label": fresh_label(),
                "actor": OP,
                **change,
            }
            with pytest.raises(ConnectError):
                await operator.create_social_org(engine, **kw)
    finally:
        await engine.dispose()
    assert org_rows(dsns, "Bad-direct") == (0, 0)


# ── signing in ───────────────────────────────────────────────────────────────


async def test_the_invited_admin_signs_in_invites_a_member_who_signs_in_too(dsns: Dsns) -> None:
    w = await social_org(make_engine(dsns.app))
    async with rig_on(dsns, w) as rig:
        first = await social_login(
            rig, GOOGLE_SUB, "ada@example.com", first_name="Ada", last_name="Lovelace"
        )
        signed_in(first, rig)
        assert await who(w, w.founder) == ("admin", "active", "Ada Lovelace")
        ada_sid = await session_of(w, w.founder)
        settings = Settings(database_dsn=dsns.app, jwks=rig.signer.jwks(), issuer=AUTH)
        with TestClient(create_app(settings)) as c:
            api = Api(c, rig.signer, b"", w.org)
            invited = api.post(
                "/v1/users/invite", api.token(w.founder, ada_sid), {"email": "bob@example.com"}
            )
            assert invited.status_code == 201, invited.text
            bob = invited.json()["id"]
            signed_in(await social_login(rig, BOB_SUB, "bob@example.com"), rig)
            assert await who(w, bob) == ("member", "active", "Pat Doe")
            as_bob = api.token(bob, await session_of(w, bob))
            assert c.get("/v1/whoami", headers=as_bob).status_code == 200
            assert_problem(
                api.post("/v1/users/invite", as_bob, {"email": "eve@example.com"}),
                ErrorCode.FORBIDDEN,
            )
            assert_problem(c.get("/v1/users", headers=as_bob), ErrorCode.FORBIDDEN)
        actions = [r[1] for r in await events_of(w.engine, w.org)]
        assert actions[:4] == ["org.created", "user.invited", "org.updated", "org.updated"]
        assert actions.count("user.invited") == 2
        assert actions.count("identity.linked") == 2
        await chain_ok(w.engine, w.org)


async def test_the_chooser_of_a_created_social_org_lists_its_providers(dsns: Dsns) -> None:
    w = await social_org(make_engine(dsns.app), "google", "microsoft")
    async with rig_on(dsns, w) as rig:
        page = await rig.browser.login(secrets.token_urlsafe(32))
        assert page.status_code == 200
        assert page.text.count("<button") == 2
        assert "value='google'" in page.text and "value='microsoft'" in page.text
        assert "GitHub" not in page.text and "Pilot" not in page.text
        pending = hidden(page.text, "pending")
        misses = [
            await rig.http.post("/login/social", data={"pending": pending, "provider": "github"}),
            await rig.http.get(
                "/login", params={"org": new_id("org"), "return_to": rig.browser.app_url()}
            ),
        ]
        assert {(r.status_code, r.text, "set-cookie" in r.headers) for r in misses} == {
            (400, pages.BAD_REQUEST, False)
        }


# ── restore-admin ────────────────────────────────────────────────────────────


async def test_restore_admin_in_a_social_org_needs_a_social_link(dsns: Dsns) -> None:
    w = await social_org(make_engine(dsns.app))
    try:
        bob = await invite(w, "bob@example.com")
        async with bound_org(w.engine, w.org) as conn:
            target = await social.resolve(conn, w.org)
            assert isinstance(target, social.SocialSignIn)
            found = await social.find_person(
                conn,
                target,
                profile(w.wo, BOB_SUB, "bob@example.com", connection_type="GoogleOAuth"),
                "google",
            )
        assert not isinstance(found, str) and found.user_id == bob
        before = await events_of(w.engine, w.org)
        with pytest.raises(operator.RestoreError, match="has not signed in yet"):
            await restore(w, w.founder)
        assert len(await events_of(w.engine, w.org)) == len(before)
        done = await restore(w, bob)
        assert (done.role_before, done.status_before, done.warnings) == ("member", "active", ())
        new = (await events_of(w.engine, w.org))[len(before) :]
        assert [(r[1], r[3], r[5]) for r in new] == [
            ("user.updated", "op_dana", bob),
            ("operator.access", "op_dana", bob),
        ]
        assert (await who(w, bob))[:2] == ("admin", "active")
        await chain_ok(w.engine, w.org)
    finally:
        await w.engine.dispose()
