"""Pilot social sign-in (decision 002 amendment 2026-10-27, plan A6), against postgres:18 and a
fake WorkOS.

  1 directory orgs unchanged; a social profile there refused -> test_a_directory_org_refuses_...
  2 an invited member signs in; each refusal one page        -> test_an_invited_member_signs_in...,
                                                                test_every_social_refusal_is_...
  3 the chooser lists the enabled providers; misses unchanged -> test_the_chooser_..., test_..._miss
  4 deactivation locks out on the next request               -> test_deactivation_locks_out_...
  5 invites: idempotent, admins only, never agents           -> test_admins_invite_people_by_email
  6 invite domains: invite, reactivate, sign-in, set-sign-in  -> test_..._outside_the_domains_...,
                                                                test_a_linked_person_is_refused_...,
                                                                test_the_operator_changes_the_...
"""

import secrets
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from ssc_testkit import Dsns, assert_problem, make_org
from test_auth_host import AUTH, SECRET, Rig, client_for, onward, run, settings
from test_identity import OPERATOR, World, new_world
from test_oauth import CONSOLE, CONSOLE_CALLBACK, OAuthRig, at_console, hidden, query

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.errors import CATALOGUE, ErrorCode
from ssc_contracts.ids import new_id
from ssc_control import directory
from ssc_control.api import Settings, create_app
from ssc_control.api.settings import USER_AUDIENCE
from ssc_control.audit.chain import Actor
from ssc_control.db import bound_org
from ssc_control.identity import __main__ as cli
from ssc_control.identity import operator, pages, sessions, social, tokens
from ssc_control.identity.authhost import AuthHost
from ssc_control.identity.cell_callers import DevCallers
from ssc_control.snapshot.service import compile_lock

ADA = "ada@example.com"
GOOGLE_SUB, GITHUB_SUB = "110248495921238986420", "583231"
BOB_SUB = "117512345678901234567"
SOCIAL_CONN = "conn_01SOCIAL"
_FORCE_SOCIAL = text(
    "update ssc.org set sign_in_mode = 'social', social_providers = cast(:providers as text[]), "
    "invite_domains = '{example.com}' where id = :org"
)


async def force_social(conn: AsyncConnection, org: str, providers: tuple[str, ...]) -> None:
    await conn.execute(_FORCE_SOCIAL, {"org": org, "providers": list(providers)})


@asynccontextmanager
async def rig_for(dsns: Dsns, providers: tuple[str, ...] = ()) -> AsyncIterator[OAuthRig]:
    w = await new_world(dsns)
    await w.tick()
    if providers:
        async with bound_org(w.engine, w.org) as conn:
            await force_social(conn, w.org, providers)
    async with rig_on(dsns, w) as rig:
        yield rig


@asynccontextmanager
async def rig_on(dsns: Dsns, w: World) -> AsyncIterator[OAuthRig]:
    (label,) = await w.rows("select cell_label from ssc.org where id = :org")
    s = settings(dsns, console_url=CONSOLE)
    signer = tokens.Signer(s.signing_pem, s.signing_kid, s.auth_url)
    workos = w.wo.client()
    host = AuthHost(s, w.engine, workos, signer, DevCallers(SECRET))
    http = client_for(host)
    try:
        yield OAuthRig(w, signer, http, Rig(w, str(label[0]), signer, host, http))
    finally:
        await http.aclose()
        await workos.aclose()
        await w.engine.dispose()


@pytest.fixture
async def rig(dsns: Dsns) -> AsyncIterator[OAuthRig]:
    async with rig_for(dsns, ("google", "github")) as r:
        yield r


async def choose(rig: OAuthRig, page: httpx2.Response, provider: str) -> httpx2.Response:
    assert page.status_code == 200, page.text
    pending = hidden(page.text, "pending")
    return await rig.http.post("/login/social", data={"pending": pending, "provider": provider})


async def social_login(  # noqa: PLR0913
    rig: OAuthRig,
    subject: str = GOOGLE_SUB,
    email: str = ADA,
    *,
    provider: str = "google",
    connection_type: str | None = None,
    **extra: Any,
) -> httpx2.Response:
    rig.http.cookies.clear()
    code = secrets.token_hex(8)
    chosen = await choose(rig, await rig.browser.login(secrets.token_urlsafe(32)), provider)
    rig.w.wo.profile(
        code,
        subject,
        email,
        connection_id=SOCIAL_CONN,
        connection_type=connection_type or social.WORKOS_PROVIDERS[provider],
        **extra,
    )
    return await rig.browser.through_workos(chosen, code)


async def invite(w: World, email: str, status: str = "invited") -> str:
    user = new_id("usr")
    await w.rows(
        "insert into ssc.user_account (id, org_id, display_name, email, role, status, "
        "deactivated_at) values (:id, :org, :email, :email, 'member', :status, "
        "case when cast(:status as text) = 'deactivated' then now() end)",
        id=user,
        email=email,
        status=status,
    )
    return user


async def reasons(w: World) -> list[str]:
    return [after["reason"] for _, _, after in await w.audit(AuditAction.LOGIN_FAILED)]


def signed_in(r: httpx2.Response, rig: OAuthRig) -> None:
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(f"https://ledger.{rig.browser.label}.apps.test/")


# ── the auth host ────────────────────────────────────────────────────────────


async def test_a_directory_org_refuses_a_social_profile(dsns: Dsns) -> None:
    async with rig_for(dsns) as rig:
        r = await rig.browser.login(secrets.token_urlsafe(32))
        assert r.status_code == 302 and "organization=" in r.headers["location"]
        rig.w.wo.profile("g", GOOGLE_SUB, ADA, connection_type="GoogleOAuth")
        refused = await rig.browser.through_workos(r, "g")
        assert (refused.status_code, refused.text) == (403, pages.REFUSED)
        assert await reasons(rig.w) == ["connection_type_refused"]
        page = await rig.http.post("/login/social", data={"pending": "x", "provider": "google"})
        assert (page.status_code, page.text) == (400, pages.BAD_REQUEST)


async def test_the_chooser_lists_only_the_enabled_providers(rig: OAuthRig) -> None:
    r = await rig.browser.login(secrets.token_urlsafe(32))
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.text.count("<button") == 2
    assert "value='google'" in r.text and "value='github'" in r.text
    assert "Microsoft" not in r.text and "Acme" not in r.text
    onward_page = await choose(rig, r, "github")
    location = onward(onward_page)
    assert location.startswith("https://workos.test/sso/authorize?")
    assert query(location)["provider"] == "GitHubOAuth"
    assert "organization" not in query(location)
    assert "ssc-login=" in onward_page.headers["set-cookie"]


async def test_a_chooser_miss_is_the_same_bad_request(rig: OAuthRig) -> None:
    page = await rig.browser.login(secrets.token_urlsafe(32))
    pending = hidden(page.text, "pending")
    misses = [
        await rig.http.post("/login/social", data={"pending": pending, "provider": "microsoft"}),
        await rig.http.post("/login/social", data={"pending": pending + "x", "provider": "google"}),
        await rig.http.post("/login/social", data={"provider": "google"}),
        await rig.http.post(
            "/login/social",
            data={"pending": pending, "provider": "google"},
            headers={"sec-fetch-site": "cross-site"},
        ),
        await rig.http.get(
            "/login", params={"org": new_id("org"), "return_to": rig.browser.app_url()}
        ),
    ]
    assert {(r.status_code, r.text, "set-cookie" in r.headers) for r in misses} == {
        (400, pages.BAD_REQUEST, False)
    }


async def test_an_invited_member_signs_in_and_becomes_active(rig: OAuthRig) -> None:
    bob = await invite(rig.w, "Bob@Example.com")
    r = await social_login(rig, BOB_SUB, "bob@example.COM")
    signed_in(r, rig)
    (row,) = await rig.w.rows(
        "select u.status, u.display_name, l.issuer, l.subject, l.source from ssc.user_account u "
        "join ssc.identity_link l on l.org_id = u.org_id and l.user_id = u.id "
        "where u.org_id = :org and u.id = :id",
        id=bob,
    )
    assert tuple(row) == ("active", "Pat Doe", "workos:social:google", BOB_SUB, "social")
    updated = await rig.w.audit(AuditAction.USER_UPDATED)
    assert updated[-1][2] == {"status": "active"}
    assert [a[2]["source"] for a in await rig.w.audit(AuditAction.IDENTITY_LINKED)] == ["social"]


async def test_an_invited_member_signs_in_with_microsoft(dsns: Dsns) -> None:
    async with rig_for(dsns, ("microsoft",)) as rig:
        bob = await invite(rig.w, "bob@example.com")
        page = await rig.browser.login(secrets.token_urlsafe(32))
        assert page.text.count("<button") == 1 and "value='microsoft'" in page.text
        signed_in(
            await social_login(rig, "AAAAAAAAAAAAAAAAAAAAAMsft", ADA, provider="microsoft"), rig
        )
        signed_in(
            await social_login(
                rig, "AAAAAAAAAAAAAAAAAAAAABob", "bob@example.com", provider="microsoft"
            ),
            rig,
        )
        (row,) = await rig.w.rows("select status from ssc.user_account where id = :id", id=bob)
        assert tuple(row) == ("active",)


async def test_github_links_only_an_address_it_says_is_verified(rig: OAuthRig) -> None:
    bob = await invite(rig.w, "bob@example.com")
    refused = await social_login(rig, GITHUB_SUB, "bob@example.com", provider="github")
    assert (refused.status_code, refused.text) == (403, pages.REFUSED)
    assert await reasons(rig.w) == ["email_not_verified"]
    assert await rig.w.rows("select 1 from ssc.identity_link where user_id = :id", id=bob) == []
    verified = {"email_verified": True}
    first = await social_login(
        rig, GITHUB_SUB, "bob@example.com", provider="github", raw_attributes=verified
    )
    signed_in(first, rig)
    signed_in(await social_login(rig, GITHUB_SUB, "bob@example.com", provider="github"), rig)


async def test_later_sign_ins_match_the_subject_only(rig: OAuthRig) -> None:
    signed_in(await social_login(rig), rig)
    await rig.w.rows(
        "update ssc.user_account set email = 'ada.renamed@example.com' where id = :id",
        id=rig.w.founder,
    )
    signed_in(await social_login(rig, email="someone@example.com"), rig)
    other = await social_login(rig, "999", "ada.renamed@example.com")
    assert (other.status_code, other.text) == (403, pages.REFUSED)
    assert await reasons(rig.w) == ["subject_mismatch"]


async def test_another_provider_never_links_to_a_linked_person(rig: OAuthRig) -> None:
    signed_in(await social_login(rig), rig)
    r = await social_login(
        rig, GITHUB_SUB, ADA, provider="github", raw_attributes={"email_verified": True}
    )
    assert (r.status_code, r.text) == (403, pages.REFUSED)
    assert await reasons(rig.w) == ["subject_mismatch"]
    links = await rig.w.rows(
        "select issuer from ssc.identity_link where org_id = :org and user_id = :id "
        "and source = 'social'",
        id=rig.w.founder,
    )
    assert [tuple(row) for row in links] == [("workos:social:google",)]


async def test_every_social_refusal_is_the_same_page(rig: OAuthRig) -> None:
    signed_in(await social_login(rig), rig)
    await invite(rig.w, "gone@example.com", "deactivated")
    attempts = [
        await social_login(rig, "1", "nobody@example.com"),
        await social_login(rig, "2", ADA, connection_type="MicrosoftOAuth"),
        await social_login(rig, "3", ADA, connection_type="OktaSAML"),
        await social_login(rig, "4", ADA, raw_attributes={"email_verified": False}),
        await social_login(rig, "5", ADA),
        await social_login(rig, "6", "gone@example.com"),
        await social_login(rig, "", ADA),
    ]
    assert {(r.status_code, r.text) for r in attempts} == {(403, pages.REFUSED)}
    assert await reasons(rig.w) == [
        "no_match",
        "provider_not_enabled",
        "connection_type_refused",
        "email_not_verified",
        "subject_mismatch",
        "not_active",
        "no_subject",
    ]
    failed = await rig.w.audit(AuditAction.LOGIN_FAILED)
    assert {(kind, target) for kind, target, _ in failed} == {("org", rig.w.org)}


async def test_a_first_sign_in_outside_the_domains_is_refused_and_links_nothing(
    rig: OAuthRig,
) -> None:
    bob = await invite(rig.w, "bob@evil.io")
    outside = ("bob@evil.io", "x@sub.example.com", "x@example.com.evil.io", "x@evilexample.com")
    attempts = [await social_login(rig, BOB_SUB, email) for email in outside]
    assert {(r.status_code, r.text) for r in attempts} == {(403, pages.REFUSED)}
    assert await reasons(rig.w) == ["email_domain_not_allowed"] * len(outside)
    assert await rig.w.rows("select 1 from ssc.identity_link where user_id = :id", id=bob) == []
    (row,) = await rig.w.rows("select status from ssc.user_account where id = :id", id=bob)
    assert tuple(row) == ("invited",)
    assert await rig.w.audit(AuditAction.IDENTITY_LINKED) == []


async def test_a_linked_person_is_refused_once_their_domain_is_removed(rig: OAuthRig) -> None:
    signed_in(await social_login(rig), rig)
    set_domains = "update ssc.org set invite_domains = cast(:d as text[]) where id = :org"
    await rig.w.rows(set_domains, d=["example.org"])
    refused = await social_login(rig)
    assert (refused.status_code, refused.text) == (403, pages.REFUSED)
    await rig.w.rows(set_domains, d=["example.com"])
    signed_in(await social_login(rig), rig)
    other = await social_login(rig, email="ada@evil.io")
    assert (other.status_code, other.text) == (403, pages.REFUSED)
    assert await reasons(rig.w) == ["email_domain_not_allowed"] * 2


async def test_a_deactivated_member_with_a_link_is_refused(rig: OAuthRig) -> None:
    bob = await invite(rig.w, "bob@example.com")
    signed_in(await social_login(rig, BOB_SUB, "bob@example.com"), rig)
    async with bound_org(rig.w.engine, rig.w.org) as conn:
        await directory.set_status(conn, rig.w.org, bob, "deactivated", actor=OPERATOR)
    r = await social_login(rig, BOB_SUB, "bob@example.com")
    assert (r.status_code, r.text) == (403, pages.REFUSED)
    assert await reasons(rig.w) == ["not_active"]


async def test_the_console_and_the_command_line_choose_too(rig: OAuthRig) -> None:
    page = await at_console(rig, org=rig.w.org)
    chosen = await choose(rig, page, "google")
    rig.w.wo.profile("c1", GOOGLE_SUB, ADA, connection_type="GoogleOAuth")
    back = await rig.browser.through_workos(chosen, "c1")
    assert back.headers["location"].startswith(f"{CONSOLE_CALLBACK}?")
    assert "code" in query(back.headers["location"])

    rig.http.cookies.clear()
    start = (await rig.http.post("/device/authorize", data={"org": rig.w.org})).json()
    form = await rig.http.post("/device", data={"org": rig.w.org, "user_code": start["user_code"]})
    chosen = await choose(rig, form, "google")
    rig.w.wo.profile("c2", GOOGLE_SUB, ADA, connection_type="GoogleOAuth")
    done = await rig.browser.through_workos(chosen, "c2")
    assert (done.status_code, done.text) == (200, pages.DEVICE_DONE)


async def test_directory_sync_skips_a_social_org(rig: OAuthRig) -> None:
    assert await rig.w.tick() is None


# ── the API ──────────────────────────────────────────────────────────────────


class Api:
    def __init__(self, client: TestClient, signer: tokens.Signer, pem: bytes, org: str) -> None:
        self.client, self.signer, self.pem, self.org = client, signer, pem, org

    def token(self, user: str, sid: str, *, agent: bool = False) -> dict[str, str]:
        access = self.signer.access_token(
            org_id=self.org,
            user_id=user,
            session_id=sid,
            audience=USER_AUDIENCE,
            now=tokens.utcnow(),
        )
        if agent:
            claims = {**jwt.decode(access, options={"verify_signature": False}), "agent": True}
            access = jwt.encode(
                claims, self.pem, algorithm="ES256", headers={"kid": "k1", "typ": "ssc-api+jwt"}
            )
        return {"authorization": f"Bearer {access}"}

    def post(self, path: str, headers: dict[str, str], body: Any = None) -> Any:
        key = {"idempotency-key": secrets.token_hex(16)}
        return self.client.post(path, json=body, headers={**headers, **key})


def jobs_waiting(dsns: Dsns, org: str) -> int:
    with psycopg.connect(dsns.superuser) as conn:
        (n,) = conn.execute(
            "select count(*) from procrastinate.procrastinate_jobs "
            "where queueing_lock = %s and status = 'todo'",
            (compile_lock(org),),
        ).fetchone() or (0,)
        return int(n)


def settle_jobs(dsns: Dsns, org: str) -> None:
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute(
            "update procrastinate.procrastinate_jobs set status = 'succeeded' "
            "where queueing_lock = %s and status = 'todo'",
            (compile_lock(org),),
        )


def social_world(dsns: Dsns) -> tuple[World, dict[str, str], str]:
    async def arrange(_: AsyncEngine) -> tuple[World, dict[str, str], str]:
        w = await new_world(dsns)
        await w.tick()
        cat = await invite(w, "cat@example.com", "active")
        async with bound_org(w.engine, w.org) as conn:
            await force_social(conn, w.org, ("google",))
            sids = {
                who: await sessions.open_session(
                    conn,
                    w.org,
                    user_id=user,
                    kind="cli",
                    connection_id=SOCIAL_CONN,
                    actor=Actor(ActorKind.USER, user),
                )
                for who, user in (("ada", w.founder), ("cat", cat))
            }
            refresh = await tokens.issue_refresh(conn, w.org, sids["cat"])
        await w.engine.dispose()
        return w, {**sids, "cat_id": cat}, refresh

    return run(dsns, arrange)


def test_admins_invite_people_by_email(dsns: Dsns) -> None:
    w, ids, _ = social_world(dsns)
    pem = tokens.new_signing_pem()
    signer = tokens.Signer(pem, "k1", AUTH)
    with TestClient(
        create_app(Settings(database_dsn=dsns.app, jwks=signer.jwks(), issuer=AUTH))
    ) as c:
        api = Api(c, signer, pem, w.org)
        ada, cat = api.token(w.founder, ids["ada"]), api.token(ids["cat_id"], ids["cat"])
        assert c.get("/v1/whoami", headers=ada).json()["sign_in_mode"] == "social"
        first = api.post("/v1/users/invite", ada, {"email": "Bob@Example.com"})
        assert first.status_code == 201, first.text
        bob = first.json()
        assert (bob["email"], bob["status"], bob["role"]) == (
            "bob@example.com",
            "invited",
            "member",
        )
        again = api.post("/v1/users/invite", ada, {"email": "bob@EXAMPLE.com"})
        assert (again.status_code, again.json()["id"]) == (200, bob["id"])
        roster = c.get("/v1/users", params={"status": "invited"}, headers=ada).json()["users"]
        assert [u["id"] for u in roster] == [bob["id"]]
        assert_problem(
            api.post("/v1/users/invite", cat, {"email": "x@example.com"}), ErrorCode.FORBIDDEN
        )
        assert_problem(
            api.post(
                "/v1/users/invite",
                api.token(w.founder, ids["ada"], agent=True),
                {"email": "x@example.com"},
            ),
            ErrorCode.AGENT_SESSION_REFUSED,
        )
        created = run(
            dsns, lambda e: World(e, w.org, w.founder, w.wo).audit(AuditAction.USER_INVITED)
        )
        assert [after for _, _, after in created][-1] == {"role": "member", "status": "invited"}

        async def to_directory(engine: AsyncEngine) -> None:
            async with bound_org(engine, w.org) as conn:
                await social.set_sign_in(conn, w.org, "directory", (), actor=OPERATOR)

        run(dsns, to_directory)
        assert c.get("/v1/whoami", headers=ada).json()["sign_in_mode"] == "directory"
        for path in ("/v1/users/invite", f"/v1/users/{ids['cat_id']}/deactivate"):
            assert_problem(
                api.post(path, ada, {"email": "z@example.com"}), ErrorCode.DIRECTORY_MANAGED
            )


def add_person(dsns: Dsns, org: str, email: str, status: str) -> str:
    user = new_id("usr")
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute(
            "insert into ssc.user_account (id, org_id, display_name, email, role, status, "
            "deactivated_at) values (%s, %s, %s, %s, 'member', %s, "
            "case when %s = 'deactivated' then now() end)",
            (user, org, email, email, status, status),
        )
    return user


def written(dsns: Dsns, org: str) -> tuple[Any, ...] | None:
    with psycopg.connect(dsns.superuser) as conn:
        return conn.execute(
            "select (select count(*) from ssc.user_account where org_id = %s), "
            "(select count(*) from ssc.audit_event where org_id = %s), "
            "(select array_agg(status order by id) from ssc.user_account where org_id = %s)",
            (org, org, org),
        ).fetchone()


def test_invites_and_reactivations_outside_the_domains_are_refused(dsns: Dsns) -> None:
    w, ids, _ = social_world(dsns)
    add_person(dsns, w.org, "old@evil.io", "active")
    gone = add_person(dsns, w.org, "gone@evil.io", "deactivated")
    pem = tokens.new_signing_pem()
    signer = tokens.Signer(pem, "k1", AUTH)
    with TestClient(
        create_app(Settings(database_dsn=dsns.app, jwks=signer.jwks(), issuer=AUTH))
    ) as c:
        api = Api(c, signer, pem, w.org)
        ada = api.token(w.founder, ids["ada"])
        assert c.get("/v1/whoami", headers=ada).json()["invite_domains"] == ["example.com"]
        before = written(dsns, w.org)
        for email in (
            "eve@evil.io",
            "Old@Evil.io",
            "eve@sub.example.com",
            "eve@example.com.evil.io",
            "eve@evilexample.com",
            "eve@ample.com",
            '"eve"@evil.io',
        ):
            r = api.post("/v1/users/invite", ada, {"email": email})
            assert_problem(r, ErrorCode.INVITE_DOMAIN_NOT_ALLOWED)
            assert (r.status_code, r.json()["detail"]) == (
                422,
                CATALOGUE[ErrorCode.INVITE_DOMAIN_NOT_ALLOWED].detail,
            ), email
        for email in ("eve@evil.io@example.com", "eve@example.com@evil.io", '"a@b"@example.com'):
            assert_problem(
                api.post("/v1/users/invite", ada, {"email": email}), ErrorCode.VALIDATION_FAILED
            )
        refused = api.post(f"/v1/users/{gone}/reactivate", ada)
        assert_problem(refused, ErrorCode.INVITE_DOMAIN_NOT_ALLOWED)
        assert_problem(api.post(f"/v1/users/{new_id('usr')}/reactivate", ada), ErrorCode.NOT_FOUND)
        assert written(dsns, w.org) == before
        for email, stored in (
            ("Zed@EXAMPLE.COM", "zed@example.com"),
            ('"Eve"@example.com', '"eve"@example.com'),
        ):
            made = api.post("/v1/users/invite", ada, {"email": email})
            assert (made.status_code, made.json()["email"]) == (201, stored)


def test_invite_and_reactivate_wait_for_a_domain_change(dsns: Dsns) -> None:
    w, ids, _ = social_world(dsns)
    gone = add_person(dsns, w.org, "gone@example.com", "deactivated")
    pem = tokens.new_signing_pem()
    signer = tokens.Signer(pem, "k1", AUTH)
    with (
        TestClient(
            create_app(Settings(database_dsn=dsns.app, jwks=signer.jwks(), issuer=AUTH))
        ) as c,
        ThreadPoolExecutor(2) as pool,
    ):
        api = Api(c, signer, pem, w.org)
        ada = api.token(w.founder, ids["ada"])
        with psycopg.connect(dsns.superuser) as held:
            held.execute("select 1 from ssc.org where id = %s for no key update", (w.org,))
            calls = [
                pool.submit(api.post, f"/v1/users/{gone}/reactivate", ada),
                pool.submit(api.post, "/v1/users/invite", ada, {"email": "new@example.com"}),
            ]
            done, _ = wait(calls, timeout=1)
            assert not done
            held.execute(
                "update ssc.org set invite_domains = '{example.org}' where id = %s", (w.org,)
            )
        for call in calls:
            assert_problem(call.result(timeout=10), ErrorCode.INVITE_DOMAIN_NOT_ALLOWED)
    with psycopg.connect(dsns.superuser) as conn:
        assert conn.execute(
            "select array_agg(email order by email) from ssc.user_account "
            "where org_id = %s and status <> 'active'",
            (w.org,),
        ).fetchone() == (["gone@example.com"],)


def test_deactivation_locks_out_on_the_next_request(dsns: Dsns) -> None:
    w, ids, refresh = social_world(dsns)
    pem = tokens.new_signing_pem()
    signer = tokens.Signer(pem, "k1", AUTH)
    other = make_org(dsns.app, "Other")
    with TestClient(
        create_app(Settings(database_dsn=dsns.app, jwks=signer.jwks(), issuer=AUTH))
    ) as c:
        api = Api(c, signer, pem, w.org)
        ada, cat = api.token(w.founder, ids["ada"]), api.token(ids["cat_id"], ids["cat"])
        assert c.get("/v1/whoami", headers=cat).status_code == 200
        assert_problem(api.post(f"/v1/users/{w.founder}/deactivate", cat), ErrorCode.FORBIDDEN)
        assert_problem(api.post(f"/v1/users/{w.founder}/deactivate", ada), ErrorCode.LAST_ORG_ADMIN)
        assert_problem(
            api.post(f"/v1/users/{other.admin_user_id}/deactivate", ada), ErrorCode.NOT_FOUND
        )
        settle_jobs(dsns, w.org)
        r = api.post(f"/v1/users/{ids['cat_id']}/deactivate", ada)
        assert (r.status_code, r.json()["status"]) == (200, "deactivated")
        assert_problem(c.get("/v1/whoami", headers=cat), ErrorCode.UNAUTHENTICATED)
        assert jobs_waiting(dsns, w.org) == 1

        async def after(engine: AsyncEngine) -> tuple[Any, ...]:
            world = World(engine, w.org, w.founder, w.wo)
            (row,) = await world.rows(
                "select sessions_not_before is not null, (select count(*) from ssc.auth_session "
                "where org_id = :org and user_id = :id and revoked_at is null) "
                "from ssc.user_account where org_id = :org and id = :id",
                id=ids["cat_id"],
            )
            host = AuthHost(settings(dsns), engine, w.wo.client(), signer, DevCallers(SECRET))
            async with client_for(host) as http:
                got = await http.post(
                    "/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
                )
            return (*row, got.status_code, got.json())

        assert run(dsns, after) == (True, 0, 400, {"error": "invalid_grant"})
        audited = run(
            dsns, lambda e: World(e, w.org, w.founder, w.wo).audit(AuditAction.USER_DEACTIVATED)
        )
        assert [(kind, target) for kind, target, _ in audited] == [("user_account", ids["cat_id"])]

        back = api.post(f"/v1/users/{ids['cat_id']}/reactivate", ada)
        assert (back.status_code, back.json()["status"]) == (200, "active")
        assert_problem(c.get("/v1/whoami", headers=cat), ErrorCode.UNAUTHENTICATED)
        assert_problem(
            api.post(
                f"/v1/users/{ids['cat_id']}/reactivate",
                api.token(w.founder, ids["ada"], agent=True),
            ),
            ErrorCode.AGENT_SESSION_REFUSED,
        )


# ── the operator command ─────────────────────────────────────────────────────


def org_updates(dsns: Dsns, org: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsns.superuser) as conn:
        return conn.execute(
            "select actor_kind, actor_id, before, after from ssc.audit_event "
            "where org_id = %s and action = 'org.updated' and actor_id = 'op_dana' order by seq",
            (org,),
        ).fetchall()


def mode_of(dsns: Dsns, org: str) -> tuple[Any, ...] | None:
    with psycopg.connect(dsns.superuser) as conn:
        return conn.execute(
            "select sign_in_mode, social_providers from ssc.org where id = %s", (org,)
        ).fetchone()


def test_the_operator_changes_a_social_orgs_providers_and_reverts_it(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    org = run(
        dsns,
        lambda e: operator.create_social_org(
            e,
            name="Pilot",
            admin_email=ADA,
            providers=("google",),
            invite_domains=("example.com",),
            cell_label="s" + secrets.token_hex(6),
            actor=OPERATOR,
        ),
    ).org_id
    base = ["set-sign-in", "--org", org, "--operator", "op_dana"]
    on = [*base, "--mode", "social", "--invite-domains", "example.com"]
    assert cli.main([*on, "--providers", "google,GitHub"]) == 0
    assert cli.main([*on, "--providers", "google,github"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{org} social", f"{org} social (unchanged)"]
    assert cli.main(on) == 2
    assert cli.main([*base, "--mode", "social", "--providers", "google"]) == 2
    assert cli.main([*base, "--mode", "directory", "--providers", "google"]) == 2
    assert cli.main([*base, "--mode", "directory", "--invite-domains", "example.com"]) == 2
    assert cli.main([*base, "--mode", "directory", "--allow-consumer-domain"]) == 2
    with pytest.raises(SystemExit):
        cli.main([*on, "--providers", "facebook"])
    assert cli.main([*base, "--mode", "directory"]) == 0
    capsys.readouterr()
    assert cli.main([*on, "--providers", "google"]) == 1
    assert "cannot move to social mode" in capsys.readouterr().err
    rows = org_updates(dsns, org)
    assert [(r[0], r[1], r[2]["social_providers"], r[3]["sign_in_mode"]) for r in rows] == [
        ("operator", "op_dana", ["google"], "social"),
        ("operator", "op_dana", ["google", "github"], "directory"),
    ]
    assert rows[0][3]["social_providers"] == ["google", "github"]
    assert mode_of(dsns, org) == ("directory", [])


def domains_of(dsns: Dsns, org: str) -> list[str]:
    with psycopg.connect(dsns.superuser) as conn:
        row = conn.execute("select invite_domains from ssc.org where id = %s", (org,)).fetchone()
    assert row is not None
    return list(row[0])


def test_the_operator_changes_the_invite_domains(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    made = run(
        dsns,
        lambda e: operator.create_social_org(
            e,
            name="Pilot",
            admin_email=ADA,
            providers=("google",),
            invite_domains=("example.com",),
            cell_label="s" + secrets.token_hex(6),
            actor=OPERATOR,
        ),
    )
    org = made.org_id
    add_person(dsns, org, "old@acme.co.uk", "deactivated")
    on = ["set-sign-in", "--org", org, "--operator", "op_dana", "--mode", "social"]
    on = [*on, "--providers", "google", "--invite-domains"]
    assert cli.main([*on, "example.com,Acme.co.uk"]) == 0
    assert domains_of(dsns, org) == ["example.com", "acme.co.uk"]
    capsys.readouterr()
    before = written(dsns, org)
    assert cli.main([*on, "acme.co.uk"]) == 1
    assert capsys.readouterr().err == (
        "not set: invited or active people are outside those domains; deactivate them first: "
        f"{made.admin_user_id} ada@example.com\n"
    )
    assert cli.main([*on, "example.com,gmail.com"]) == 1
    assert "gmail.com is a consumer mail domain" in capsys.readouterr().err
    assert written(dsns, org) == before
    assert domains_of(dsns, org) == ["example.com", "acme.co.uk"]
    assert cli.main([*on, "example.com,gmail.com", "--allow-consumer-domain"]) == 0
    assert cli.main([*on, "example.com"]) == 0
    assert domains_of(dsns, org) == ["example.com"]
    assert (
        cli.main(["set-sign-in", "--org", org, "--operator", "op_dana", "--mode", "directory"]) == 0
    )
    assert domains_of(dsns, org) == []
    rows = org_updates(dsns, org)
    assert [(r[2]["invite_domains"], r[3]) for r in rows] == [
        (
            ["example.com"],
            {
                "sign_in_mode": "social",
                "social_providers": ["google"],
                "invite_domains": ["example.com", "acme.co.uk"],
            },
        ),
        (
            ["example.com", "acme.co.uk"],
            {
                "sign_in_mode": "social",
                "social_providers": ["google"],
                "invite_domains": ["example.com", "gmail.com"],
                "allow_consumer_domain": True,
            },
        ),
        (
            ["example.com", "gmail.com"],
            {
                "sign_in_mode": "social",
                "social_providers": ["google"],
                "invite_domains": ["example.com"],
            },
        ),
        (
            ["example.com"],
            {"sign_in_mode": "directory", "social_providers": [], "invite_domains": []},
        ),
    ]


def test_a_directory_org_is_refused_social_mode_and_nothing_is_written(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    org = make_org(dsns.app, "Directory").org_id
    with psycopg.connect(dsns.superuser) as conn:
        count = "select count(*) from ssc.audit_event where org_id = %s"
        before = conn.execute(count, (org,)).fetchone()
    argv = ["set-sign-in", "--org", org, "--operator", "op_dana", "--mode", "social"]
    assert cli.main([*argv, "--providers", "google", "--invite-domains", "example.com"]) == 1
    err = capsys.readouterr().err
    assert err == (
        f"not set: {org} is a directory org and cannot move to social mode; a social pilot is "
        "a new org, made with create-social-org\n"
    )

    async def direct(engine: AsyncEngine) -> None:
        async with bound_org(engine, org) as conn:
            await social.set_sign_in(
                conn, org, "social", ("google",), actor=OPERATOR, invite_domains=("example.com",)
            )

    with pytest.raises(social.SignInError, match="cannot move to social mode"):
        run(dsns, direct)
    assert cli.main([*argv[:-1], "directory"]) == 0
    assert capsys.readouterr().out == f"{org} directory (unchanged)\n"
    with psycopg.connect(dsns.superuser) as conn:
        assert conn.execute(count, (org,)).fetchone() == before
    assert mode_of(dsns, org) == ("directory", [])
