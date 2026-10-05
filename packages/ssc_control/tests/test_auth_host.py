"""SSC-019: the auth host end to end over ASGI, against a real postgres:18 and a fake WorkOS.

Browser sign-in for app hosts (login, WorkOS, the hand-back, redeeming the code as the gateway),
the command line (device flow, refresh, revoke) and the API refusing a credential once its
session has ended.
"""

import asyncio
import base64
import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns, assert_problem
from test_identity import BOB_IDP, BOB_UID, FOUNDER_IDP, World, new_world, profile

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_control.api import Settings, create_app
from ssc_control.api.auth import Verifier
from ssc_control.api.settings import USER_AUDIENCE
from ssc_control.audit.chain import Actor
from ssc_control.db import NewOrg, bound_org, create_org, make_engine
from ssc_control.identity import join, pages, sessions, tokens
from ssc_control.identity.authhost import DEVICE_GRANT, AuthHost, create_auth_app
from ssc_control.identity.cell_callers import CallerCheck, CellCaller, DevCallers
from ssc_control.identity.settings import AuthSettings
from ssc_shared.hosts import cell_project

AUTH = "https://auth.test"
DOMAIN = "apps.test"
SECRET = "s" * 32


def binding_of(nonce: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(nonce.encode()).digest()).rstrip(b"=").decode()


@dataclass
class FixedCallers:
    project: str | None

    async def caller(self, bearer: str) -> CellCaller | None:
        return CellCaller(self.project) if bearer == "google.id.token" else None


@dataclass
class Rig:
    w: World
    label: str
    signer: tokens.Signer
    host: AuthHost
    http: httpx2.AsyncClient

    def app_url(self, app: str = "ledger", path: str = "/books?y=1") -> str:
        return f"https://{app}.{self.label}.{DOMAIN}{path}"

    async def login(self, nonce: str, app: str = "ledger") -> httpx2.Response:
        params = {"org": self.w.org, "return_to": self.app_url(app), "binding": binding_of(nonce)}
        return await self.http.get("/login", params=params)

    async def through_workos(self, r: httpx2.Response, code: str) -> httpx2.Response:
        assert r.status_code == 302, r.text
        q = parse_qs(urlsplit(r.headers["location"]).query)
        return await self.http.get("/callback", params={"code": code, "state": q["state"][0]})

    async def redeem(
        self, location: str, nonce: str, bearer: str = f"dev.{SECRET}", http: Any = None
    ) -> httpx2.Response:
        parts = urlsplit(location)
        code = parse_qs(parts.query)["code"][0]
        body = {"org": self.w.org, "code": code, "host": parts.hostname, "nonce": nonce}
        client = http or self.http
        return await client.post(
            "/internal/redeem", json=body, headers={"authorization": f"Bearer {bearer}"}
        )

    async def sign_in(self, app: str = "ledger") -> tuple[str, str]:
        """A full browser sign-in; the hand-back location and the nonce."""
        nonce = secrets.token_urlsafe(32)
        self.w.wo.profile("okta-code", FOUNDER_IDP, "ada@example.com")
        r = await self.login(nonce, app)
        if r.headers["location"].startswith("https://workos.test/"):
            r = await self.through_workos(r, "okta-code")
        return r.headers["location"], nonce


def settings(dsns: Dsns, **kw: Any) -> AuthSettings:
    base: dict[str, Any] = {
        "database_dsn": dsns.app,
        "workos_api_key": "sk_test_fake",
        "workos_client_id": "client_fake",
        "signing_pem": tokens.new_signing_pem(),
        "signing_kid": "k1",
        "state_key": secrets.token_bytes(32),
        "auth_url": AUTH,
        "apps_domain": DOMAIN,
        "workos_base": "https://workos.test",
        "environment": "test",
        "dev_cell_secret": SECRET,
    }
    return AuthSettings(**{**base, **kw})


def client_for(host: AuthHost) -> httpx2.AsyncClient:
    transport = httpx2.ASGITransport(app=create_auth_app(host))
    return httpx2.AsyncClient(transport=transport, base_url=AUTH)


@pytest.fixture
async def rig(dsns: Dsns) -> AsyncIterator[Rig]:
    w = await new_world(dsns)
    await w.tick()
    (label,) = await w.rows("select cell_label from ssc.org where id = :org")
    s = settings(dsns)
    signer = tokens.Signer(s.signing_pem, s.signing_kid, s.auth_url)
    workos = w.wo.client()
    host = AuthHost(s, w.engine, workos, signer, DevCallers(SECRET))
    http = client_for(host)
    try:
        yield Rig(w, str(label[0]), signer, host, http)
    finally:
        await http.aclose()
        await workos.aclose()
        await w.engine.dispose()


# ── browser sign-in for app hosts ────────────────────────────────────────────


async def test_a_person_signs_in_to_an_app_host(rig: Rig) -> None:
    nonce = secrets.token_urlsafe(32)
    r = await rig.login(nonce)
    assert r.status_code == 302
    authorize = urlsplit(r.headers["location"])
    q = parse_qs(authorize.query)
    assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == (
        "https://workos.test/sso/authorize"
    )
    assert q["organization"] == [rig.w.wo.organization]
    assert q["redirect_uri"] == [f"{AUTH}/callback"]
    assert "__Host-ssc-login=" in r.headers["set-cookie"]

    rig.w.wo.profile("okta-code", FOUNDER_IDP, "ada@example.com")
    back = await rig.through_workos(r, "okta-code")
    assert back.status_code == 302
    location = urlsplit(back.headers["location"])
    assert (location.scheme, location.hostname, location.path) == (
        "https",
        f"ledger.{rig.label}.{DOMAIN}",
        "/.ssc/callback",
    )
    assert parse_qs(location.query)["next"] == ["/books?y=1"]
    cookies = back.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-ssc-auth=") and "Max-Age=43200" in c for c in cookies)

    done = await rig.redeem(back.headers["location"], nonce)
    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["sub"], body["org"], body["email"]) == (
        rig.w.founder,
        rig.w.org,
        "ada@example.com",
    )
    assert 0 < body["exp"] - body["iat"] <= sessions.SESSION_SECONDS
    again = await rig.redeem(back.headers["location"], nonce)
    assert (again.status_code, again.json()) == (400, {"error": "invalid_grant"})
    successes = await rig.w.audit(AuditAction.LOGIN_SUCCEEDED)
    assert [r[0] for r in successes] == ["auth_session"]


async def test_the_next_app_host_skips_workos_until_logout(rig: Rig) -> None:
    await rig.sign_in()
    nonce = secrets.token_urlsafe(32)
    r = await rig.login(nonce, "payroll")
    assert r.status_code == 302
    location = r.headers["location"]
    assert urlsplit(location).hostname == f"payroll.{rig.label}.{DOMAIN}"
    assert (await rig.redeem(location, nonce)).status_code == 200

    out = await rig.http.get("/logout")
    assert (out.status_code, out.text) == (200, pages.SIGNED_OUT)
    r = await rig.login(secrets.token_urlsafe(32), "payroll")
    assert r.headers["location"].startswith("https://workos.test/")


async def test_a_code_is_only_good_with_its_host_and_nonce(rig: Rig) -> None:
    location, nonce = await rig.sign_in()
    assert (await rig.redeem(location, "another-browser")).status_code == 400
    assert (await rig.redeem(location, nonce)).status_code == 400, "a wrong try uses it up"
    location, nonce = await rig.sign_in()
    moved = location.replace(f"ledger.{rig.label}", f"payroll.{rig.label}")
    assert (await rig.redeem(moved, nonce)).status_code == 400


@pytest.mark.parametrize(
    "change",
    [
        {"org": "org_nope"},
        {"binding": "short"},
        {"return_to": "https://evil.test/"},
        {"return_to": "http://ledger.LABEL.apps.test/"},
        {"return_to": "https://ledger.othercll.apps.test/"},
        {"return_to": "https://ledger.LABEL.apps.test:8443/"},
        {"return_to": "https://user@ledger.LABEL.apps.test/"},
        {"return_to": "https://ledger.LABEL.apps.test//evil.test"},
    ],
)
async def test_a_bad_login_link_is_refused(rig: Rig, change: dict[str, str]) -> None:
    params = {"org": rig.w.org, "return_to": rig.app_url(), "binding": binding_of("n")}
    params.update({k: v.replace("LABEL", rig.label) for k, v in change.items()})
    r = await rig.http.get("/login", params=params)
    assert (r.status_code, r.text) == (400, pages.BAD_REQUEST)


async def test_a_callback_without_its_login_cookie_or_state_is_refused(rig: Rig) -> None:
    r = await rig.login(secrets.token_urlsafe(32))
    q = parse_qs(urlsplit(r.headers["location"]).query)
    wrong = await rig.http.get("/callback", params={"code": "x", "state": "forged"})
    assert wrong.status_code == 400
    async with client_for(rig.host) as fresh:
        cold = await fresh.get("/callback", params={"code": "x", "state": q["state"][0]})
    assert cold.status_code == 400


async def test_a_sign_in_workos_refused_shows_the_refused_page(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    r = await rig.login(secrets.token_urlsafe(32))
    q = parse_qs(urlsplit(r.headers["location"]).query)
    error = {"error": "profile_not_allowed_outside_organization", "state": q["state"][0]}
    with caplog.at_level(logging.WARNING, logger="ssc.auth"):
        refused = await rig.http.get("/callback", params=error)
    assert (refused.status_code, refused.text) == (403, pages.REFUSED)
    assert "profile_not_allowed_outside_organization" in caplog.text
    forged = await rig.http.get("/callback", params={**error, "state": "forged"})
    assert forged.status_code == 400


@pytest.mark.parametrize(
    ("idp_id", "connection_type", "reason"),
    [
        (FOUNDER_IDP, "GoogleOAuth", "connection_type_refused"),
        (FOUNDER_IDP, "MagicLink", "connection_type_refused"),
        ("00ustranger", "OktaSAML", "no_match"),
        ("", "OktaSAML", "no_subject"),
    ],
)
async def test_every_refused_sign_in_looks_the_same(
    rig: Rig, idp_id: str, connection_type: str, reason: str
) -> None:
    rig.w.wo.profile("bad", idp_id, "ada@example.com", connection_type=connection_type)
    r = await rig.through_workos(await rig.login(secrets.token_urlsafe(32)), "bad")
    assert (r.status_code, r.text) == (403, pages.REFUSED)
    assert "set-cookie" not in r.headers or "__Host-ssc-auth=" not in r.headers["set-cookie"]
    failed = await rig.w.audit(AuditAction.LOGIN_FAILED)
    assert [(f[0], f[2]) for f in failed] == [("directory_connection", {"reason": reason})]


async def test_a_deactivated_person_cannot_sign_in_or_reuse_a_browser_session(rig: Rig) -> None:
    await rig.sign_in()
    async with bound_org(rig.w.engine, rig.w.org) as conn:
        await sessions.revoke_user(
            conn, rig.w.org, rig.w.founder, "operator", actor=Actor(ActorKind.OPERATOR, "op")
        )
    r = await rig.login(secrets.token_urlsafe(32), "payroll")
    assert r.headers["location"].startswith("https://workos.test/"), "old session not reused"


async def test_only_the_org_cell_may_redeem(rig: Rig) -> None:
    location, nonce = await rig.sign_in()
    assert (await rig.redeem(location, nonce, bearer="dev.wrong")).status_code == 401
    assert (await rig.redeem(location, nonce, bearer="")).status_code == 401
    other = AuthHost(
        rig.host.settings, rig.w.engine, rig.host.workos, rig.signer, FixedCallers("ssc-c-other")
    )
    async with client_for(other) as http:
        r = await rig.redeem(location, nonce, bearer="google.id.token", http=http)
        assert r.status_code == 401
    mine = AuthHost(
        rig.host.settings,
        rig.w.engine,
        rig.host.workos,
        rig.signer,
        FixedCallers(cell_project(rig.label)),
    )
    async with client_for(mine) as http:
        r = await rig.redeem(location, nonce, bearer="google.id.token", http=http)
        assert r.status_code == 200


async def test_redeem_from_the_label_project_passes_with_no_cell_project_column(rig: Rig) -> None:
    (stored,) = await rig.w.rows("select cell_project from ssc.org where id = :org")
    assert stored[0] is None
    location, nonce = await rig.sign_in()
    mine = AuthHost(
        rig.host.settings,
        rig.w.engine,
        rig.host.workos,
        rig.signer,
        FixedCallers(f"ssc-c-{rig.label}"),
    )
    async with client_for(mine) as http:
        r = await rig.redeem(location, nonce, bearer="google.id.token", http=http)
    assert r.status_code == 200


async def test_redeem_from_another_cells_project_is_refused(rig: Rig) -> None:
    location, nonce = await rig.sign_in()
    other = AuthHost(
        rig.host.settings,
        rig.w.engine,
        rig.host.workos,
        rig.signer,
        FixedCallers(cell_project("bcdfghjkmnpq")),
    )
    async with client_for(other) as http:
        r = await rig.redeem(location, nonce, bearer="google.id.token", http=http)
    assert r.status_code == 401
    assert (await rig.redeem(location, nonce)).status_code == 200, "the refused try kept the code"


async def test_jwks_and_health(rig: Rig) -> None:
    assert (await rig.http.get("/healthz")).json() == {"status": "ok"}
    keys = (await rig.http.get("/.well-known/jwks.json")).json()["keys"]
    assert [(k["kid"], k["alg"], k["crv"]) for k in keys] == [("k1", "ES256", "P-256")]
    assert "d" not in keys[0]


# ── the command line ─────────────────────────────────────────────────────────


async def device_login(rig: Rig) -> dict[str, Any]:
    start = (await rig.http.post("/device/authorize", data={"org": rig.w.org})).json()
    assert start["interval"] == tokens.DEVICE_INTERVAL
    assert start["verification_uri"] == f"{AUTH}/device?org={rig.w.org}"
    pending = await rig.http.post(
        "/token", data={"grant_type": DEVICE_GRANT, "device_code": start["device_code"]}
    )
    assert (pending.status_code, pending.json()) == (400, {"error": "authorization_pending"})
    page = await rig.http.get(start["verification_uri_complete"].removeprefix(AUTH))
    assert start["user_code"] in page.text
    r = await rig.http.post("/device", data={"org": rig.w.org, "user_code": start["user_code"]})
    rig.w.wo.profile("cli-code", FOUNDER_IDP, "ada@example.com")
    done = await rig.through_workos(r, "cli-code")
    assert (done.status_code, done.text) == (200, pages.DEVICE_DONE)
    got = await rig.http.post(
        "/token", data={"grant_type": DEVICE_GRANT, "device_code": start["device_code"]}
    )
    assert got.status_code == 200, got.text
    return got.json()


async def test_the_command_line_signs_in_with_the_device_flow(rig: Rig) -> None:
    got = await device_login(rig)
    assert got["token_type"] == "Bearer" and got["expires_in"] == tokens.ACCESS_SECONDS
    principal = Verifier(rig.signer.jwks(), AUTH).verify(got["access_token"], USER_AUDIENCE)
    assert principal.subject == rig.w.founder and principal.org_id == rig.w.org
    assert principal.session_id is not None and principal.session_id.startswith("ses_")
    assert principal.credential_id == principal.session_id
    live = await rig.w.live(principal.session_id)
    assert live is not None and live.kind == "cli"
    issued = await rig.w.audit(AuditAction.TOKEN_ISSUED)
    assert [i[2] for i in issued] == [{"kind": "cli", "via": "device"}]


async def test_refresh_rotates_and_reuse_ends_the_session(rig: Rig) -> None:
    got = await device_login(rig)
    first = got["refresh_token"]
    r = await rig.http.post("/token", data={"grant_type": "refresh_token", "refresh_token": first})
    assert r.status_code == 200
    second = r.json()["refresh_token"]
    reuse = await rig.http.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": first}
    )
    assert reuse.json() == {"error": "invalid_grant"}
    r = await rig.http.post("/token", data={"grant_type": "refresh_token", "refresh_token": second})
    assert r.json() == {"error": "invalid_grant"}, "the reuse ended the whole session"


async def test_logout_revokes_the_command_line_session(rig: Rig) -> None:
    got = await device_login(rig)
    sid = Verifier(rig.signer.jwks(), AUTH).verify(got["access_token"], USER_AUDIENCE).session_id
    assert sid is not None
    r = await rig.http.post("/revoke", data={"token": got["refresh_token"]})
    assert r.status_code == 200
    assert await rig.w.live(sid) is None
    assert (await rig.http.post("/revoke", data={"token": "junk"})).status_code == 200


async def test_the_device_form_refuses_a_cross_site_post_and_unknown_codes(rig: Rig) -> None:
    start = (await rig.http.post("/device/authorize", data={"org": rig.w.org})).json()
    form = {"org": rig.w.org, "user_code": start["user_code"]}
    cross = await rig.http.post("/device", data=form, headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 400
    unknown = await rig.http.post("/device", data={"org": rig.w.org, "user_code": "BBBBBBBB"})
    assert unknown.status_code == 400
    assert (await rig.http.post("/token", data={"grant_type": "password"})).json() == {
        "error": "unsupported_grant_type"
    }
    bad = await rig.http.post("/device/authorize", data={"org": "nope"})
    assert bad.json() == {"error": "invalid_request"}


async def test_an_agent_login_names_the_agent_and_every_token_carries_it(rig: Rig) -> None:
    """SSC-048: ``ssc login --agent claude-code``. The person confirms the agent by name before
    single sign-on; the session, its audit rows and every access token (refreshed too) say it."""
    start = (
        await rig.http.post("/device/authorize", data={"org": rig.w.org, "agent": "claude-code"})
    ).json()
    form = {"org": rig.w.org, "user_code": start["user_code"]}
    consent = await rig.http.post("/device", data=form)
    assert consent.status_code == 200
    assert "claude-code" in consent.text and "name=agent value='claude-code'" in consent.text
    spoofed = await rig.http.post("/device", data={**form, "agent": "other"})
    assert spoofed.status_code == 200 and "name=agent value='claude-code'" in spoofed.text
    r = await rig.http.post("/device", data={**form, "agent": "claude-code"})
    rig.w.wo.profile("agent-code", FOUNDER_IDP, "ada@example.com")
    done = await rig.through_workos(r, "agent-code")
    assert (done.status_code, done.text) == (200, pages.DEVICE_DONE)
    got = await rig.http.post(
        "/token", data={"grant_type": DEVICE_GRANT, "device_code": start["device_code"]}
    )
    assert got.status_code == 200, got.text
    verifier = Verifier(rig.signer.jwks(), AUTH)
    principal = verifier.verify(got.json()["access_token"], USER_AUDIENCE)
    assert principal.subject == rig.w.founder
    assert principal.is_agent and principal.client_id == "claude-code"
    assert principal.session_id is not None
    live = await rig.w.live(principal.session_id)
    assert live is not None and live.kind == "cli" and live.agent_client_id == "claude-code"
    issued = await rig.w.audit(AuditAction.TOKEN_ISSUED)
    assert [i[2] for i in issued] == [{"kind": "cli", "via": "device", "client_id": "claude-code"}]
    logins = await rig.w.audit(AuditAction.LOGIN_SUCCEEDED)
    assert logins[-1][2]["client_id"] == "claude-code"
    refresh = {"grant_type": "refresh_token", "refresh_token": got.json()["refresh_token"]}
    again = await rig.http.post("/token", data=refresh)
    assert again.status_code == 200
    renewed = verifier.verify(again.json()["access_token"], USER_AUDIENCE)
    assert renewed.is_agent and renewed.client_id == "claude-code"


async def test_a_person_login_is_never_an_agent_and_a_bad_agent_name_is_refused(rig: Rig) -> None:
    got = await device_login(rig)
    principal = Verifier(rig.signer.jwks(), AUTH).verify(got["access_token"], USER_AUDIENCE)
    assert not principal.is_agent and principal.client_id is None
    for name in ("Claude Code", "x" * 65, "-lead", "a/b"):
        bad = await rig.http.post("/device/authorize", data={"org": rig.w.org, "agent": name})
        assert bad.json() == {"error": "invalid_request"}, name


# ── the API ──────────────────────────────────────────────────────────────────


def run[T](dsns: Dsns, fn: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def go() -> T:
        engine = make_engine(dsns.app)
        try:
            return await fn(engine)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_the_api_refuses_a_credential_once_its_session_ends(dsns: Dsns) -> None:
    signer = tokens.Signer(tokens.new_signing_pem(), "k1", AUTH)
    api = Settings(database_dsn=dsns.app, jwks=signer.jwks(), issuer=AUTH)

    async def arrange(engine: AsyncEngine) -> tuple[str, str, str]:
        created = await create_org(
            engine, NewOrg("Acme", "Ada", "ada@example.com", "workos:directory_01X", "00u1")
        )
        async with bound_org(engine, created.org_id) as conn:
            sid = await sessions.open_session(
                conn,
                created.org_id,
                user_id=created.admin_user_id,
                kind="cli",
                connection_id="conn_01X",
                actor=Actor(ActorKind.USER, created.admin_user_id),
            )
        return created.org_id, created.admin_user_id, sid

    org, user, sid = run(dsns, arrange)

    def token(subject: str = user) -> dict[str, str]:
        access = signer.access_token(
            org_id=org, user_id=subject, session_id=sid, audience=USER_AUDIENCE, now=tokens.utcnow()
        )
        return {"authorization": f"Bearer {access}"}

    async def revoke(engine: AsyncEngine) -> None:
        async with bound_org(engine, org) as conn:
            await sessions.revoke_session(
                conn, org, sid, "logout", actor=Actor(ActorKind.USER, user)
            )

    with TestClient(create_app(api)) as client:
        r = client.get("/v1/whoami", headers=token())
        assert r.status_code == 200 and r.json()["credential_id"] == sid
        assert_problem(
            client.get("/v1/whoami", headers=token("usr_" + "z" * 20)), ErrorCode.UNAUTHENTICATED
        )
        run(dsns, revoke)
        assert_problem(client.get("/v1/whoami", headers=token()), ErrorCode.UNAUTHENTICATED)
        assert_problem(
            client.get("/v1/audit/export", params={"format": "csv"}, headers=token()),
            ErrorCode.UNAUTHENTICATED,
        )


def test_an_admin_links_an_unlinked_login(dsns: Dsns) -> None:
    pem = tokens.new_signing_pem()
    signer = tokens.Signer(pem, "k1", AUTH)
    api = Settings(database_dsn=dsns.app, jwks=signer.jwks(), issuer=AUTH)
    stranger = "00ustranger"

    async def arrange(_: AsyncEngine) -> tuple[World, dict[str, str]]:
        w = await new_world(dsns)
        w.wo.user(BOB_UID, BOB_IDP, "bob@example.com", first="Bob")
        await w.tick()
        sids: dict[str, str] = {}
        async with bound_org(w.engine, w.org) as conn:
            for name, subject in (("ada", FOUNDER_IDP), ("bob", BOB_IDP)):
                user = (await w.person(subject))[0]
                sids[name] = await sessions.open_session(
                    conn,
                    w.org,
                    user_id=user,
                    kind="cli",
                    connection_id=w.wo.sso,
                    actor=Actor(ActorKind.USER, user),
                )
            for idp_id in (stranger, "00uother"):
                found = await join.find_person(
                    conn, await w.connection(), profile(w.wo, idp_id, f"{idp_id}@example.com")
                )
                assert found == "no_match"
        await w.rows(
            "insert into ssc.unlinked_login (id, org_id, connection_id, subject, email, reason) "
            "values ('ulg_' || repeat('a', 20), :org, :conn, 'eve@example.com', "
            "'eve@example.com', 'no_match')",
            conn=w.wo.sso,
        )
        await w.engine.dispose()
        return w, sids

    w, sids = run(dsns, arrange)
    ada = run(dsns, lambda e: World(e, w.org, w.founder, w.wo).person(FOUNDER_IDP))[0]
    bob = run(dsns, lambda e: World(e, w.org, w.founder, w.wo).person(BOB_IDP))[0]

    def token(user: str, sid: str, **extra: Any) -> dict[str, str]:
        access = signer.access_token(
            org_id=w.org, user_id=user, session_id=sid, audience=USER_AUDIENCE, now=tokens.utcnow()
        )
        if extra:
            claims = {**jwt.decode(access, options={"verify_signature": False}), **extra}
            access = jwt.encode(
                claims, pem, algorithm="ES256", headers={"kid": "k1", "typ": "ssc-api+jwt"}
            )
        return {"authorization": f"Bearer {access}"}

    admin = token(ada, sids["ada"])

    def link(ulg: str, user: str, headers: dict[str, str] = admin) -> Any:
        return client.post(
            f"/v1/unlinked-logins/{ulg}/link",
            json={"user_id": user},
            headers={**headers, "idempotency-key": secrets.token_hex(16)},
        )

    with TestClient(create_app(api)) as client:
        assert_problem(
            client.get("/v1/unlinked-logins", headers=token(bob, sids["bob"])), ErrorCode.FORBIDDEN
        )
        listed = client.get("/v1/unlinked-logins", headers=admin).json()["unlinked_logins"]
        by_subject = {u["subject"]: u for u in listed}
        assert set(by_subject) == {stranger, "00uother", "eve@example.com"}
        assert by_subject[stranger]["reason"] == "no_match"
        assert by_subject[stranger]["linkable"] and not by_subject["eve@example.com"]["linkable"]
        target = by_subject[stranger]["id"]

        assert_problem(
            link(target, bob, token(ada, sids["ada"], agent=True)), ErrorCode.AGENT_SESSION_REFUSED
        )
        assert_problem(link(target, bob, token(bob, sids["bob"])), ErrorCode.FORBIDDEN)
        assert_problem(link(target, "usr_" + "z" * 20), ErrorCode.REFERENCE_NOT_FOUND)
        assert_problem(link(by_subject["eve@example.com"]["id"], bob), ErrorCode.VALIDATION_FAILED)
        r = link(target, bob)
        assert r.status_code == 200, r.text
        assert r.json()["user_id"] == bob
        assert_problem(link(target, bob), ErrorCode.NOT_FOUND)
        after = client.get("/v1/unlinked-logins", headers=admin).json()["unlinked_logins"]
        assert stranger not in {u["subject"] for u in after}

    async def login_again(engine: AsyncEngine) -> object:
        again = World(engine, w.org, w.founder, w.wo)
        async with bound_org(engine, w.org) as conn:
            return await join.find_person(
                conn, await again.connection(), profile(w.wo, stranger, "s@example.com")
            )

    assert run(dsns, login_again) == join.Person(bob, active=True)
    linked = run(
        dsns, lambda e: World(e, w.org, w.founder, w.wo).audit(AuditAction.IDENTITY_LINKED)
    )
    assert [row[2]["source"] for row in linked][-1] == "admin"


def test_settings_refuse_dev_shortcuts_in_production(dsns: Dsns) -> None:
    for kw in ({"auth_url": "http://auth.test"}, {"dev_cell_secret": SECRET}):
        with pytest.raises(ValueError, match="dev and test"):
            settings(dsns, environment="prod", **({"dev_cell_secret": ""} | kw))
    assert settings(dsns, auth_url="http://localhost:8001").secure_cookies is False
    with pytest.raises(ValueError, match="32"):
        DevCallers("short")


async def test_dev_callers_need_the_exact_secret() -> None:
    callers: CallerCheck = DevCallers(SECRET)
    assert await callers.caller(f"dev.{SECRET}") == CellCaller(None)
    for wrong in (SECRET, f"dev.{SECRET}x", "dev.", ""):
        assert await callers.caller(wrong) is None
