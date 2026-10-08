"""SSC gap 7 (and the console's half of gap 1): the auth host's OAuth authorization server end to
end over ASGI, against a real postgres:18 and a fake WorkOS (decision 029).

A remote MCP client registers itself and sends the person to ``/authorize``. The org is found
from a work email, the person signs in through WorkOS and consents, and the code that comes back
becomes an MCP credential: the API's agent interface takes it and ``/v1`` refuses it from
outside. The console is the first-party client: no consent, a ``console`` session, the API's user
audience.

Brief checks:
  * the whole flow with fake WorkOS          -> test_an_mcp_client_signs_in_and_calls_the_...
  * PKCE wrong verifier                       -> test_a_code_needs_its_verifier_client_...
  * code reuse revokes                        -> test_a_code_presented_again_ends_its_session
  * wrong redirect                            -> test_an_unknown_client_or_redirect_is_never_...
  * wrong resource                            -> test_a_bad_request_goes_back_to_the_client_...
  * expired code                              -> test_an_expired_code_is_refused
  * enumeration-equal responses               -> test_every_miss_looks_the_same
  * MCP refuses a user-audience token and /v1
    an MCP-audience token from outside        -> test_an_mcp_client_signs_in_and_calls_the_...
  * CORS for the console origin only          -> test_only_the_console_origin_calls_token_...
"""

import base64
import hashlib
import html
import logging
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from ssc_testkit import Dsns
from test_auth_host import AUTH, SECRET, Rig, client_for, onward, settings
from test_identity import FOUNDER_IDP, OPERATOR, World, new_world

from ssc_contracts.audit import AuditAction
from ssc_control.api import Settings, create_app
from ssc_control.api.auth import Verifier
from ssc_control.api.problems import Refusal
from ssc_control.api.settings import USER_AUDIENCE
from ssc_control.db import bound_org
from ssc_control.identity import authorize, connections, oauth, pages, tokens
from ssc_control.identity import jobs as identity_jobs
from ssc_control.identity.authhost import AuthHost
from ssc_control.identity.cell_callers import DevCallers
from ssc_control.identity.limits import RateLimit, client_address
from ssc_control.worker import build_app

API = "http://testserver"
MCP = f"{API}/mcp"
CONSOLE = "https://console.test"
CONSOLE_CALLBACK = f"{CONSOLE}/auth/callback"
REDIRECT = "http://127.0.0.1:43117/callback"
STATE = "st-123"
EMAIL = "ada@acme.example"
_AGE_CLIENT = text(
    "update ssc.oauth_client set created_at = now() - interval '31 days' where client_id = :id"
)
JSONRPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-11-25",
}


def pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def hidden(page: str, name: str) -> str:
    found = re.search(rf"name={name} value='([^']*)'", page)
    assert found is not None, page
    return html.unescape(found.group(1))


def query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def forwarded(client: str) -> dict[str, str]:
    """As the control load balancer passes a request on: a spoofed entry, the client, itself."""
    return {"x-forwarded-for": f"6.6.6.6, {client}, 35.191.0.1"}


@dataclass
class OAuthRig:
    w: World
    signer: tokens.Signer
    http: httpx2.AsyncClient
    browser: Rig

    async def register(self, name: str = "Claude Code", uris: tuple[str, ...] = ()) -> str:
        body = {"client_name": name, "redirect_uris": list(uris or ("http://127.0.0.1/callback",))}
        r = await self.http.post("/register", json=body)
        assert r.status_code == 201, r.text
        return str(r.json()["client_id"])

    async def authorize(self, client_id: str, challenge: str, **change: str) -> httpx2.Response:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": STATE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": MCP,
        } | change
        return await self.http.get("/authorize", params={k: v for k, v in params.items() if v})

    async def email(
        self, page: httpx2.Response, email: str = EMAIL, **headers: str
    ) -> httpx2.Response:
        assert page.status_code == 200, page.text
        body = {"pending": hidden(page.text, "pending"), "email": email}
        return await self.http.post("/authorize", data=body, headers=headers)

    async def through_workos(self, r: httpx2.Response) -> httpx2.Response:
        location = onward(r)
        assert location.startswith("https://workos.test/sso/authorize")
        self.w.wo.profile("oauth-code", FOUNDER_IDP, "ada@example.com")
        state = query(location)["state"]
        return await self.http.get("/callback", params={"code": "oauth-code", "state": state})

    async def consent(self, page: httpx2.Response, answer: str = "approve") -> httpx2.Response:
        assert page.status_code == 200, page.text
        body = {"answer_token": hidden(page.text, "answer_token"), "answer": answer}
        return await self.http.post("/authorize/consent", data=body)

    async def code(self, client_id: str, challenge: str) -> str:
        """A whole sign-in for a registered client, approved; the code it got back."""
        r = await self.consent(
            await self.through_workos(await self.email(await self.authorize(client_id, challenge)))
        )
        assert r.status_code == 302, r.text
        back = query(r.headers["location"])
        assert (back["state"], back["iss"]) == (STATE, AUTH)
        return back["code"]

    async def token(
        self, client_id: str, code: str, verifier: str, **change: str
    ) -> httpx2.Response:
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "resource": MCP,
        } | change
        return await self.http.post("/token", data={k: v for k, v in form.items() if v})

    async def refresh(self, refresh_token: str) -> httpx2.Response:
        return await self.http.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    def verify(self, access: str, audience: str = MCP) -> Any:
        return Verifier(self.signer.jwks(), AUTH).verify(access, audience)


@pytest.fixture
async def rig(dsns: Dsns) -> AsyncIterator[OAuthRig]:
    w = await new_world(dsns)
    await w.tick()
    w.wo.domains = {"acme.example": "verified", "pending.example": "pending"}
    (label,) = await w.rows("select cell_label from ssc.org where id = :org")
    s = settings(dsns, mcp_resource=MCP, console_url=CONSOLE)
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


# ── the whole flow ───────────────────────────────────────────────────────────


async def test_the_metadata_names_every_endpoint(rig: OAuthRig) -> None:
    r = await rig.http.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    assert meta["issuer"] == AUTH
    assert meta["authorization_endpoint"] == f"{AUTH}/authorize"
    assert meta["token_endpoint"] == f"{AUTH}/token"
    assert meta["registration_endpoint"] == f"{AUTH}/register"
    assert meta["revocation_endpoint"] == f"{AUTH}/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert "authorization_code" in meta["grant_types_supported"]


async def test_an_mcp_client_signs_in_and_calls_the_agent_interface(  # noqa: PLR0915  (one journey)
    rig: OAuthRig, dsns: Dsns
) -> None:
    client_id = await rig.register(uris=("http://127.0.0.1:1/callback",))
    verifier, challenge = pkce()

    page = await rig.authorize(client_id, challenge)
    assert page.status_code == 200 and "work email" in page.text
    consent = await rig.through_workos(await rig.email(page))
    assert consent.status_code == 200, consent.text
    assert "Claude Code" in consent.text and "127.0.0.1" in consent.text and "Acme" in consent.text
    assert "http://127.0.0.1:*" in consent.headers["content-security-policy"]
    assert any(c.startswith("__Host-ssc-consent=") for c in consent.headers.get_list("set-cookie"))
    assert await rig.w.audit(AuditAction.AUTHORIZE_APPROVED) == []

    back = await rig.consent(consent)
    assert back.status_code == 302
    location = back.headers["location"]
    assert location.startswith(f"{REDIRECT}?"), "any loopback port (RFC 8252)"
    answer = query(location)
    assert (answer["state"], answer["iss"]) == (STATE, AUTH)
    assert answer["code"].startswith(f"ac.{rig.w.org}.")

    got = await rig.token(client_id, answer["code"], verifier)
    assert got.status_code == 200, got.text
    assert got.headers["cache-control"] == "no-store"
    body = got.json()
    principal = rig.verify(body["access_token"])
    assert (principal.org_id, principal.subject) == (rig.w.org, rig.w.founder)
    assert principal.is_agent and principal.client_id == "claude-code"
    with pytest.raises(Refusal):
        rig.verify(body["access_token"], USER_AUDIENCE)
    assert principal.session_id is not None
    live = await rig.w.live(principal.session_id)
    assert live is not None and live.kind == "cli" and live.agent_client_id == "claude-code"

    approved = await rig.w.audit(AuditAction.AUTHORIZE_APPROVED)
    assert [(a[0], a[1]) for a in approved] == [("oauth_client", client_id)]
    assert approved[0][2]["redirect_host"] == "127.0.0.1"
    assert approved[0][2]["session_id"] == principal.session_id
    issued = await rig.w.audit(AuditAction.TOKEN_ISSUED)
    assert issued[-1][2]["via"] == "authorization_code"

    refreshed = await rig.refresh(body["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    again = rig.verify(refreshed.json()["access_token"])
    assert again.session_id == principal.session_id and again.client_id == "claude-code"

    user_audience = rig.signer.access_token(
        org_id=rig.w.org,
        user_id=rig.w.founder,
        session_id=principal.session_id,
        audience=USER_AUDIENCE,
        now=tokens.utcnow(),
        agent_client_id="claude-code",
    )
    api = Settings(
        database_dsn=dsns.app,
        jwks=rig.signer.jwks(),
        issuer=AUTH,
        public_url=API,
        environment="test",
    )
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_apps", "arguments": {}},
    }
    mcp_bearer = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    v1_bearer = {"Authorization": f"Bearer {user_audience}"}
    with TestClient(create_app(api), base_url=API) as client:
        meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == MCP and meta["authorization_servers"] == [AUTH]
        r = client.post("/mcp", json=call, headers={**mcp_bearer, **JSONRPC_HEADERS})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["isError"] is False
        r = client.post("/mcp", json=call, headers={**v1_bearer, **JSONRPC_HEADERS})
        assert r.status_code == 401, "a /v1 credential never gets into the agent interface"
        assert client.get("/v1/whoami", headers=mcp_bearer).status_code == 401
        assert client.get("/v1/whoami", headers=v1_bearer).status_code == 200


async def test_a_signed_in_browser_skips_the_email_step(rig: OAuthRig) -> None:
    await rig.browser.sign_in()
    r = await rig.authorize(await rig.register(), pkce()[1])
    consent = await rig.through_workos(r)
    assert consent.status_code == 200 and "answer_token" in consent.text


async def test_the_console_signs_in_without_consent(rig: OAuthRig) -> None:
    verifier, challenge = pkce()
    console = {"client_id": oauth.CONSOLE_CLIENT_ID, "redirect_uri": CONSOLE_CALLBACK}
    page = await rig.authorize(challenge=challenge, resource=USER_AUDIENCE, **console)
    back = await rig.through_workos(await rig.email(page))
    assert back.status_code == 302, back.text
    assert back.headers["location"].startswith(f"{CONSOLE_CALLBACK}?")
    answer = query(back.headers["location"])
    assert (answer["state"], answer["iss"]) == (STATE, AUTH)

    got = await rig.token(code=answer["code"], verifier=verifier, resource=USER_AUDIENCE, **console)
    assert got.status_code == 200, got.text
    principal = rig.verify(got.json()["access_token"], USER_AUDIENCE)
    assert principal.subject == rig.w.founder and not principal.is_agent
    assert principal.session_id is not None
    live = await rig.w.live(principal.session_id)
    assert live is not None and live.kind == "console" and live.agent_client_id is None
    refreshed = await rig.refresh(got.json()["refresh_token"])
    assert rig.verify(refreshed.json()["access_token"], USER_AUDIENCE).session_id == live.id
    assert await rig.w.audit(AuditAction.AUTHORIZE_APPROVED) != []

    wrong = await rig.authorize(challenge=challenge, **console)
    assert query(wrong.headers["location"])["error"] == "invalid_target"


async def test_only_the_console_origin_calls_token_and_revoke_cross_origin(
    rig: OAuthRig,
) -> None:
    preflight = {"origin": CONSOLE, "access-control-request-method": "POST"}
    for path in ("/token", "/revoke"):
        ok = await rig.http.options(path, headers=preflight)
        assert ok.status_code == 204
        assert ok.headers["access-control-allow-origin"] == CONSOLE
        assert ok.headers["access-control-allow-methods"] == "POST"
        assert "access-control-allow-credentials" not in ok.headers
        other = await rig.http.options(path, headers={**preflight, "origin": "https://evil.test"})
        assert other.status_code == 400
        assert "access-control-allow-origin" not in other.headers

    answer = await rig.http.post(
        "/token", data={"grant_type": "refresh_token"}, headers={"origin": CONSOLE}
    )
    assert answer.status_code == 400 and answer.json() == {"error": "invalid_grant"}
    assert answer.headers["access-control-allow-origin"] == CONSOLE
    assert "access-control-allow-credentials" not in answer.headers
    assert "Origin" in answer.headers["vary"]
    revoked = await rig.http.post("/revoke", data={"token": "x"}, headers={"origin": CONSOLE})
    assert revoked.headers["access-control-allow-origin"] == CONSOLE
    for origin in ("https://evil.test", f"{CONSOLE}.evil.test", "null"):
        refused = await rig.http.post("/revoke", data={"token": "x"}, headers={"origin": origin})
        assert "access-control-allow-origin" not in refused.headers
    elsewhere = await rig.http.get("/.well-known/jwks.json", headers={"origin": CONSOLE})
    assert "access-control-allow-origin" not in elsewhere.headers
    preflight_elsewhere = await rig.http.options("/authorize", headers=preflight)
    assert "access-control-allow-origin" not in preflight_elsewhere.headers


async def test_a_denied_client_hears_access_denied(rig: OAuthRig) -> None:
    client_id = await rig.register()
    consent = await rig.through_workos(await rig.email(await rig.authorize(client_id, pkce()[1])))
    r = await rig.consent(consent, "deny")
    assert r.status_code == 302
    assert query(r.headers["location"]) == {"error": "access_denied", "state": STATE, "iss": AUTH}
    assert any(c.startswith('__Host-ssc-consent=""') for c in r.headers.get_list("set-cookie"))
    denied = await rig.w.audit(AuditAction.AUTHORIZE_DENIED)
    assert [(d[0], d[1]) for d in denied] == [("oauth_client", client_id)]
    assert await rig.w.audit(AuditAction.AUTHORIZE_APPROVED) == []
    opened = await rig.w.rows("select count(*) from ssc.auth_session where org_id = :org")
    assert opened[0][0] == 0


async def test_consent_needs_its_cookie_and_a_same_site_post(rig: OAuthRig) -> None:
    client_id = await rig.register()
    consent = await rig.through_workos(await rig.email(await rig.authorize(client_id, pkce()[1])))
    answer = {"answer_token": hidden(consent.text, "answer_token"), "answer": "approve"}
    cross = await rig.http.post(
        "/authorize/consent", data=answer, headers={"sec-fetch-site": "cross-site"}
    )
    assert (cross.status_code, cross.text) == (400, pages.BAD_REQUEST)
    odd = await rig.http.post("/authorize/consent", data={**answer, "answer": "yes"})
    assert odd.status_code == 400
    rig.http.cookies.clear()
    r = await rig.http.post("/authorize/consent", data=answer)
    assert (r.status_code, r.text) == (400, pages.BAD_REQUEST)
    assert await rig.w.audit(AuditAction.AUTHORIZE_APPROVED) == []


# ── refusals ─────────────────────────────────────────────────────────────────


async def test_an_unknown_client_or_redirect_is_never_redirected_to(rig: OAuthRig) -> None:
    client_id = await rig.register()
    challenge = pkce()[1]
    for r in (
        await rig.authorize(client_id, challenge, redirect_uri="https://evil.example/callback"),
        await rig.authorize(client_id, challenge, redirect_uri="http://127.0.0.1:9/other"),
        await rig.authorize("A" * 22, challenge),
        await rig.authorize("not a client", challenge),
        await rig.authorize(oauth.CONSOLE_CLIENT_ID, challenge),
        await rig.http.get(
            "/authorize",
            params=[("client_id", client_id), ("client_id", client_id), ("redirect_uri", REDIRECT)],
        ),
    ):
        assert (r.status_code, r.text) == (400, pages.UNKNOWN_CLIENT)
        assert "location" not in r.headers


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": ""}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"resource": f"{API}/other"}, "invalid_target"),
        ({"resource": USER_AUDIENCE}, "invalid_target"),
        ({"resource": ""}, "invalid_target"),
    ],
)
async def test_a_bad_request_goes_back_to_the_client_as_an_error(
    rig: OAuthRig, change: dict[str, str], error: str
) -> None:
    r = await rig.authorize(await rig.register(), pkce()[1], **change)
    assert r.status_code == 302
    assert r.headers["location"].startswith(f"{REDIRECT}?")
    assert query(r.headers["location"]) == {"error": error, "state": STATE, "iss": AUTH}


async def test_a_request_without_state_is_refused(rig: OAuthRig) -> None:
    r = await rig.authorize(await rig.register(), pkce()[1], state="")
    assert query(r.headers["location"]) == {"error": "invalid_request", "iss": AUTH}


async def test_a_code_needs_its_verifier_client_redirect_and_resource(rig: OAuthRig) -> None:
    client_id, other = await rig.register(), await rig.register("Other")
    verifier, challenge = pkce()
    for change in (
        {"code_verifier": pkce()[0]},
        {"client_id": other},
        {"redirect_uri": "http://127.0.0.1:43117/elsewhere"},
        {"resource": USER_AUDIENCE},
    ):
        code = await rig.code(client_id, challenge)
        r = await rig.token(
            change.get("client_id", client_id),
            code,
            change.get("code_verifier", verifier),
            **{k: v for k, v in change.items() if k not in {"client_id", "code_verifier"}},
        )
        assert (r.status_code, r.json()) == (400, {"error": "invalid_grant"}), change
        retry = await rig.token(client_id, code, verifier)
        assert retry.json() == {"error": "invalid_grant"}, "a wrong try uses the code up"
    code = await rig.code(client_id, challenge)
    missing = await rig.token(client_id, code, "")
    assert missing.json() == {"error": "invalid_request"}
    unknown = await rig.token("B" * 22, code, verifier)
    assert (unknown.status_code, unknown.json()) == (401, {"error": "invalid_client"})
    junk = await rig.token(client_id, "ac.nonsense", verifier)
    assert junk.json() == {"error": "invalid_grant"}
    assert (await rig.token(client_id, code, verifier)).status_code == 200


async def test_a_code_presented_again_ends_its_session(
    rig: OAuthRig, caplog: pytest.LogCaptureFixture
) -> None:
    client_id = await rig.register()
    verifier, challenge = pkce()
    code = await rig.code(client_id, challenge)
    first = await rig.token(client_id, code, verifier)
    assert first.status_code == 200
    sid = rig.verify(first.json()["access_token"]).session_id
    assert sid is not None and await rig.w.live(sid) is not None

    with caplog.at_level(logging.WARNING, logger="ssc.auth"):
        again = await rig.token(client_id, code, verifier)
    assert (again.status_code, again.json()) == (400, {"error": "invalid_grant"})
    assert await rig.w.live(sid) is None
    (reason,) = await rig.w.rows(
        "select revoke_reason from ssc.auth_session where org_id = :org and id = :sid", sid=sid
    )
    assert reason[0] == "code_reuse"
    reused = await rig.w.audit(AuditAction.CODE_REUSED)
    assert reused == [("auth_session", sid, {"client_id": client_id})]
    assert any("presented again" in r.getMessage() for r in caplog.records)
    refresh = await rig.refresh(first.json()["refresh_token"])
    assert refresh.json() == {"error": "invalid_grant"}


async def test_an_expired_code_is_refused(rig: OAuthRig) -> None:
    client_id = await rig.register()
    verifier, challenge = pkce()
    code = await rig.code(client_id, challenge)
    await rig.w.rows(
        "update ssc.oauth_code set created_at = now() - interval '3 minutes', "
        "expires_at = now() - interval '2 minutes' where org_id = :org"
    )
    r = await rig.token(client_id, code, verifier)
    assert (r.status_code, r.json()) == (400, {"error": "invalid_grant"})


# ── finding the org ──────────────────────────────────────────────────────────


async def test_every_miss_looks_the_same(rig: OAuthRig) -> None:
    page = await rig.authorize(await rig.register(), pkce()[1])
    misses = [
        await rig.email(page, "bob@nowhere.example"),
        await rig.email(page, "bob@pending.example"),
        await rig.email(page, "not an email"),
        await rig.email(page, ""),
    ]
    workos_org = rig.w.wo.organization
    rig.w.wo.organization = "org_elsewhere"
    misses.append(await rig.email(page))
    rig.w.wo.organization = workos_org
    async with bound_org(rig.w.engine, rig.w.org) as conn:
        assert await connections.freeze(conn, rig.w.org, "operator", actor=OPERATOR)
    misses.append(await rig.email(page))
    for r in misses:
        assert (r.status_code, r.text) == (200, pages.NO_SIGN_IN)
        assert "set-cookie" not in r.headers and "location" not in r.headers
    assert len({tuple(sorted(r.headers.keys())) for r in misses}) == 1


async def test_the_email_step_is_limited_per_address_and_per_domain(rig: OAuthRig) -> None:
    page = await rig.authorize(await rig.register(), pkce()[1])
    for i in range(20):
        r = await rig.email(page, f"x@d{i}.example", **forwarded("10.0.0.1"))
        assert r.status_code == 200
    r = await rig.email(page, "x@another.example", **forwarded("10.0.0.1"))
    assert (r.status_code, r.text) == (429, pages.TOO_MANY)
    for i in range(20):
        r = await rig.email(page, "x@one.example", **forwarded(f"10.0.1.{i}"))
        assert r.status_code == 200
    blocked = await rig.email(page, "x@one.example", **forwarded("10.0.2.1"))
    assert blocked.status_code == 429, "twenty tries for one domain, from any address"
    other = await rig.email(page, "x@two.example", **forwarded("10.0.2.1"))
    assert other.status_code == 200


async def test_workos_down_says_so(rig: OAuthRig) -> None:
    page = await rig.authorize(await rig.register(), pkce()[1])
    rig.w.wo.fail = 500
    r = await rig.email(page)
    assert (r.status_code, r.text) == (503, pages.UNAVAILABLE)


async def test_the_email_form_refuses_a_cross_site_post_and_a_bad_pending(rig: OAuthRig) -> None:
    page = await rig.authorize(await rig.register(), pkce()[1])
    cross = await rig.email(page, **{"sec-fetch-site": "cross-site"})
    assert (cross.status_code, cross.text) == (400, pages.BAD_REQUEST)
    r = await rig.http.post("/authorize", data={"pending": "forged", "email": EMAIL})
    assert (r.status_code, r.text) == (400, pages.BAD_REQUEST)


def test_the_client_address_is_the_one_the_load_balancer_saw() -> None:
    assert client_address("6.6.6.6, 1.2.3.4, 35.191.0.1", "10.0.0.9", 2) == "1.2.3.4"
    assert client_address("1.2.3.4, 35.191.0.1", "10.0.0.9", 2) == "1.2.3.4"
    assert client_address("35.191.0.1", "10.0.0.9", 2) == "10.0.0.9"
    assert client_address("6.6.6.6, 1.2.3.4", "10.0.0.9", 0) == "10.0.0.9"
    assert client_address(None, None, 2) == "unknown"


def test_a_rate_limit_slides_and_stays_bounded() -> None:
    now = [0.0]
    limit = RateLimit(lambda: now[0], 2, window=10.0, max_tracked=2)
    assert limit.allow("a") and limit.allow("a") and not limit.allow("a")
    now[0] = 10.0
    assert limit.allow("a"), "the window slid"
    assert limit.allow("b")
    now[0] = 25.0
    assert limit.allow("c") and limit.allow("c") and not limit.allow("c")


# ── registration ─────────────────────────────────────────────────────────────


async def test_a_client_registers_with_no_secret(rig: OAuthRig) -> None:
    body = {
        "client_name": "  Cursor  ",
        "redirect_uris": ["https://cursor.example/callback", "com.example.app:/oauth"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
    }
    r = await rig.http.post("/register", json=body)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["client_name"] == "Cursor" and len(got["client_id"]) == 22
    assert got["token_endpoint_auth_method"] == "none" and "client_secret" not in got
    assert got["redirect_uris"] == body["redirect_uris"]
    unnamed = await rig.http.post("/register", json={"redirect_uris": [REDIRECT]})
    assert unnamed.json()["client_name"] == authorize.UNNAMED_CLIENT


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        ({"redirect_uris": "https://a.example/cb"}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://evil.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://a.example/cb#frag"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://*.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["javascript:alert(1)"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://user@a.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": [REDIRECT] * 11}, "invalid_redirect_uri"),
        ({"redirect_uris": [REDIRECT], "client_name": ""}, "invalid_client_metadata"),
        ({"redirect_uris": [REDIRECT], "client_name": "x" * 101}, "invalid_client_metadata"),
        ({"redirect_uris": [REDIRECT], "client_name": "a\nb"}, "invalid_client_metadata"),
        (
            {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_basic"},
            "invalid_client_metadata",
        ),
        ({"redirect_uris": [REDIRECT], "grant_types": ["implicit"]}, "invalid_client_metadata"),
        ({"redirect_uris": [REDIRECT], "response_types": ["token"]}, "invalid_client_metadata"),
        (["not", "an", "object"], "invalid_client_metadata"),
    ],
)
async def test_a_bad_registration_is_refused(rig: OAuthRig, body: object, error: str) -> None:
    r = await rig.http.post("/register", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == error


async def test_registration_is_limited_per_address(rig: OAuthRig) -> None:
    body = {"redirect_uris": [REDIRECT]}
    for _ in range(10):
        r = await rig.http.post("/register", json=body, headers=forwarded("10.1.0.1"))
        assert r.status_code == 201
    r = await rig.http.post("/register", json=body, headers=forwarded("10.1.0.1"))
    assert r.status_code == 429 and r.headers["retry-after"] == "3600"
    r = await rig.http.post("/register", json=body, headers=forwarded("10.1.0.2"))
    assert r.status_code == 201
    junk = await rig.http.post("/register", content=b"{", headers=forwarded("10.1.0.3"))
    assert junk.json()["error"] == "invalid_client_metadata"


async def test_unused_clients_are_pruned_daily(rig: OAuthRig) -> None:
    old, fresh = await rig.register("Old"), await rig.register("Fresh")
    async with rig.w.engine.begin() as conn:
        await conn.execute(_AGE_CLIENT, {"id": old})
    assert await identity_jobs.prune_clients(rig.w.engine) >= 1
    r = await rig.authorize(old, pkce()[1])
    assert (r.status_code, r.text) == (400, pages.UNKNOWN_CLIENT)
    assert (await rig.authorize(fresh, pkce()[1])).status_code == 200

    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert identity_jobs.PRUNE_TASK in app.tasks
    periodic = [
        p
        for key, p in app.periodic_registry.periodic_tasks.items()
        if key[0] == identity_jobs.PRUNE_TASK
    ]
    assert len(periodic) == 1
