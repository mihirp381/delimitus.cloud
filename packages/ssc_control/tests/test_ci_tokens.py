"""GA-7.7: CI tokens. A person's command-line login asks the auth host for a ``preview``-scoped
access token backed by a ``ci`` session, for a repository secret.

The auth host mints it (only it holds the signing key) and refuses agents, scoped credentials and
sessions that are not a person's own live command-line login.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import jwt
import pytest
from ssc_testkit import Dsns
from test_auth_host import AUTH, SECRET, Rig, client_for, device_login, settings
from test_identity import new_world

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.api.auth import CredentialScope, Verifier
from ssc_control.api.settings import USER_AUDIENCE
from ssc_control.audit.chain import Actor
from ssc_control.db import bound_org
from ssc_control.identity import sessions, tokens
from ssc_control.identity.authhost import AuthHost
from ssc_control.identity.cell_callers import DevCallers
from ssc_control.identity.limits import CI_TOKENS_PER_HOUR

NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}


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


async def open_session(
    rig: Rig,
    kind: sessions.SessionKind = "cli",
    *,
    user: str | None = None,
    agent: str | None = None,
    audience: str | None = None,
) -> str:
    who = user or rig.w.founder
    async with bound_org(rig.w.engine, rig.w.org) as conn:
        return await sessions.open_session(
            conn,
            rig.w.org,
            user_id=who,
            kind=kind,
            connection_id=rig.w.wo.sso,
            actor=Actor(ActorKind.USER, who),
            agent_client_id=agent,
            token_audience=audience,
        )


def access(rig: Rig, sid: str, *, user: str | None = None, **kw: Any) -> str:
    return rig.signer.access_token(
        org_id=rig.w.org,
        user_id=user or rig.w.founder,
        session_id=sid,
        audience=USER_AUDIENCE,
        now=tokens.utcnow(),
        **kw,
    )


async def create(
    rig: Rig, bearer: str | None, body: object = None, **headers: str
) -> httpx2.Response:
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    payload = {"label": "acme/ledger"} if body is None else body
    return await rig.http.post("/ci-tokens", json=payload, headers=headers)


async def ci_rows(rig: Rig) -> list[Any]:
    return await rig.w.rows(
        "select id, kind, scope, label, connection_id, agent_client_id, token_audience, "
        "expires_at - created_at from ssc.auth_session where org_id = :org and kind = 'ci'"
    )


# ── minting ─────────────────────────────────────────────────────────────────


async def test_a_person_creates_a_ci_token_from_their_command_line_login(rig: Rig) -> None:
    login = await device_login(rig)
    before = datetime.now(UTC)
    r = await create(rig, login["access_token"], {"label": "  acme/ledger  "})
    assert r.status_code == 200, r.text
    for name, value in NO_STORE.items():
        assert r.headers[name] == value
    body = r.json()
    assert set(body) == {"token", "id", "label", "expires_at"}
    assert body["label"] == "acme/ledger" and body["id"].startswith("ses_")
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert body["expires_at"].endswith("Z")
    assert abs(expires_at - before - timedelta(days=90)) < timedelta(minutes=1)

    principal = Verifier(rig.signer.jwks(), AUTH).verify(body["token"], USER_AUDIENCE)
    assert principal.scope is CredentialScope.PREVIEW
    assert principal.session_id == principal.credential_id == body["id"]
    assert principal.subject == rig.w.founder and principal.org_id == rig.w.org
    assert not principal.is_agent and principal.client_id is None
    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert claims["exp"] == int(expires_at.timestamp())

    ((sid, kind, scope, label, conn, agent, audience, lifetime),) = await ci_rows(rig)
    assert (sid, kind, scope, label) == (body["id"], "ci", "preview", "acme/ledger")
    assert (conn, agent, audience, lifetime) == (rig.w.wo.sso, None, None, timedelta(days=90))
    refresh = await rig.w.rows(
        "select count(*) from ssc.refresh_token where org_id = :org and session_id = :sid", sid=sid
    )
    assert refresh == [(0,)]
    live = await rig.w.live(sid)
    assert live is not None and live.kind == "ci"

    issued = await rig.w.audit(AuditAction.TOKEN_ISSUED)
    assert issued[-1][:2] == ("auth_session", sid)
    assert issued[-1][2] == {
        "kind": "ci",
        "scope": "preview",
        "user_id": rig.w.founder,
        "expires_at": issued[-1][2]["expires_at"],
    }
    assert datetime.fromisoformat(issued[-1][2]["expires_at"]) == expires_at
    everything = await rig.w.rows("select after::text from ssc.audit_event where org_id = :org")
    assert not [a for (a,) in everything if a and ("acme/ledger" in a or body["token"] in a)]


async def test_a_ci_token_lasts_the_days_asked_for(rig: Rig) -> None:
    sid = await open_session(rig)
    r = await create(rig, access(rig, sid), {"label": "nightly", "days": 1})
    assert r.status_code == 200, r.text
    ((_, _, _, _, _, _, _, lifetime),) = await ci_rows(rig)
    assert lifetime == timedelta(days=1)


async def test_a_ci_token_outlives_the_login_that_made_it(rig: Rig) -> None:
    sid = await open_session(rig)
    r = await create(rig, access(rig, sid))
    async with bound_org(rig.w.engine, rig.w.org) as conn:
        await sessions.revoke_session(
            conn, rig.w.org, sid, "logout", actor=Actor(ActorKind.USER, rig.w.founder)
        )
    assert await rig.w.live(r.json()["id"]) is not None


# ── refusals ────────────────────────────────────────────────────────────────


async def refused_without_a_session(rig: Rig, r: httpx2.Response, status: int, error: str) -> None:
    assert (r.status_code, r.json()) == (status, {"error": error}), r.text
    assert r.headers["cache-control"] == "no-store"
    assert await ci_rows(rig) == []


@pytest.mark.parametrize("bearer", [None, "", "not.a.token"])
async def test_no_valid_bearer_is_refused(rig: Rig, bearer: str | None) -> None:
    r = await create(rig, bearer)
    await refused_without_a_session(rig, r, 401, "invalid_token")


async def test_another_issuers_token_is_refused(rig: Rig) -> None:
    sid = await open_session(rig)
    other = tokens.Signer(tokens.new_signing_pem(), "k1", AUTH)
    token = other.access_token(
        org_id=rig.w.org,
        user_id=rig.w.founder,
        session_id=sid,
        audience=USER_AUDIENCE,
        now=tokens.utcnow(),
    )
    await refused_without_a_session(rig, await create(rig, token), 401, "invalid_token")


async def test_an_agents_login_is_refused(rig: Rig) -> None:
    sid = await open_session(rig, agent="claude-code")
    token = access(rig, sid, agent_client_id="claude-code")
    await refused_without_a_session(rig, await create(rig, token), 403, "access_denied")


async def test_a_scoped_credential_is_refused_a_ci_token_included(rig: Rig) -> None:
    sid = await open_session(rig)
    scoped = access(rig, sid, scope="preview")
    await refused_without_a_session(rig, await create(rig, scoped), 403, "access_denied")
    async with bound_org(rig.w.engine, rig.w.org) as conn:
        opened = await sessions.open_ci_session(
            conn,
            rig.w.org,
            parent_id=sid,
            user_id=rig.w.founder,
            label="ci",
            days=1,
            actor=Actor(ActorKind.USER, rig.w.founder),
        )
    assert opened is not None
    ci = access(rig, opened[0], scope="preview", expires_at=opened[1])
    r = await create(rig, ci)
    assert (r.status_code, r.json()) == (403, {"error": "access_denied"})
    assert len(await ci_rows(rig)) == 1


@pytest.mark.parametrize("kind", ["browser", "console"])
async def test_a_session_that_is_not_a_command_line_login_is_refused(
    rig: Rig, kind: sessions.SessionKind
) -> None:
    sid = await open_session(rig, kind)
    await refused_without_a_session(rig, await create(rig, access(rig, sid)), 403, "access_denied")


async def test_an_mcp_clients_login_is_refused(rig: Rig) -> None:
    sid = await open_session(rig, audience=f"{USER_AUDIENCE}/mcp")
    await refused_without_a_session(rig, await create(rig, access(rig, sid)), 403, "access_denied")


async def test_a_token_naming_someone_elses_session_is_refused(rig: Rig) -> None:
    sid = await open_session(rig)
    token = access(rig, sid, user="usr_" + "z" * 20)
    await refused_without_a_session(rig, await create(rig, token), 401, "invalid_token")


async def test_a_revoked_or_expired_login_is_refused(rig: Rig) -> None:
    revoked = await open_session(rig)
    async with bound_org(rig.w.engine, rig.w.org) as conn:
        await sessions.revoke_session(
            conn, rig.w.org, revoked, "logout", actor=Actor(ActorKind.USER, rig.w.founder)
        )
    await refused_without_a_session(
        rig, await create(rig, access(rig, revoked)), 401, "invalid_token"
    )
    expired = await open_session(rig)
    token = access(rig, expired)
    await rig.w.rows(
        "update ssc.auth_session set created_at = now() - interval '12 hours 1 second', "
        "expires_at = now() - interval '1 second' where org_id = :org and id = :id",
        id=expired,
    )
    await refused_without_a_session(rig, await create(rig, token), 401, "invalid_token")


@pytest.mark.parametrize(
    "body",
    [
        {"label": "x", "days": 0},
        {"label": "x", "days": 91},
        {"label": "x", "days": "90"},
        {"label": "x", "days": True},
        {"label": "x", "days": 1.5},
        {"label": ""},
        {"label": "   "},
        {"label": "x" * 101},
        {"label": "two\nlines"},
        {"label": "tab\there"},
        {"label": 7},
        {"days": 30},
        {"label": "x", "scope": "prod"},
        ["acme"],
        "acme",
    ],
)
async def test_a_bad_request_is_refused(rig: Rig, body: object) -> None:
    sid = await open_session(rig)
    r = await create(rig, access(rig, sid), body)
    await refused_without_a_session(rig, r, 400, "invalid_request")


async def test_a_body_over_4_kib_is_refused(rig: Rig) -> None:
    sid = await open_session(rig)
    r = await rig.http.post(
        "/ci-tokens",
        content=b'{"label": "x", "pad": "' + b"a" * 4096 + b'"}',
        headers={"authorization": f"Bearer {access(rig, sid)}"},
    )
    await refused_without_a_session(rig, r, 400, "invalid_request")


async def test_a_person_makes_at_most_ten_an_hour(rig: Rig) -> None:
    sid = await open_session(rig)
    token = access(rig, sid)
    for _ in range(CI_TOKENS_PER_HOUR):
        assert (await create(rig, token)).status_code == 200
    r = await create(rig, token)
    assert r.status_code == 429 and r.headers["retry-after"] == "3600"
    assert r.json()["error"] == "invalid_request"
    assert len(await ci_rows(rig)) == CI_TOKENS_PER_HOUR


async def test_the_route_answers_no_cross_origin_call(rig: Rig) -> None:
    sid = await open_session(rig)
    r = await create(rig, access(rig, sid), origin=rig.host.settings.console_url)
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
