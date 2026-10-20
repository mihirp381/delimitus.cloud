"""T4: an agent attacks the API and the agent interface, offline, against the ``test_mcp`` rig
with two orgs (``adversarial_rig``). The live sweep is ``ssc_conformance.adversarial``."""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx2
import psycopg
import pytest
from adversarial_rig import (
    PLANTED_DATABASE,
    PLANTED_HOST,
    SHAPED,
    Attacker,
    ShapedLogs,
    Victim,
    add_release,
    attacker,
    call,
    fingerprint,
    login,
    made,
    victim,
)
from fake_workos import FakeWorkOS
from ssc_testkit import Dsns, SigningKey, new_key
from test_auth_host import SECRET, client_for
from test_auth_host import settings as auth_settings
from test_mcp import CLIENT_ID, JSONRPC_HEADERS, env_id, rows, rpc_call, serving, settings

from ssc_conformance import adversarial
from ssc_conformance.adversarial import KNOWN, got_of, problem, send
from ssc_contracts.ids import new_id
from ssc_control.api.mcp.tools import TOOLS
from ssc_control.api.openapi import build_spec
from ssc_control.api.settings import USER_AUDIENCE
from ssc_control.db import make_engine
from ssc_control.identity import tokens
from ssc_control.identity.authhost import AuthHost
from ssc_control.identity.cell_callers import DevCallers

SPEC = build_spec()
MARKED = sorted(adversarial.marked(SPEC))
ZERO = "0" * 20
NOT_THERE = ("NOT_FOUND", "APP_NOT_FOUND", "REFERENCE_NOT_FOUND", "FORBIDDEN")


@dataclass(frozen=True)
class Arena:
    dsns: Dsns
    key: SigningKey
    url: str
    a: Attacker
    b: Victim

    def snapshot(self) -> dict[str, str]:
        return fingerprint(self.dsns.superuser, (self.a.org.org_id, self.b.org.org_id))

    def v1(self, token: str) -> httpx2.Client:
        return httpx2.Client(
            base_url=self.url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )

    def rpc(self, name: str, args: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        """One ``tools/call`` as the attacker's agent: the JSON-RPC result."""
        r = mcp_post(self.url, token or self.a.mcp.access, rpc_call(name, args))
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        assert "result" in body, body
        result: dict[str, Any] = body["result"]
        return result


def mcp_post(url: str, token: str, body: dict[str, Any]) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {token}", **JSONRPC_HEADERS}
    return httpx2.post(f"{url}/mcp", json=body, headers=headers, timeout=60)


@pytest.fixture(scope="module")
def arena(
    dsns: Dsns, signing_key: SigningKey, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Arena]:
    blobs = tmp_path_factory.mktemp("blobs")
    with serving(settings(dsns, signing_key, 100_000, 1000.0), blobs, ShapedLogs()) as url:
        a, b = attacker(url, dsns, signing_key), victim(url, dsns, signing_key)
        yield Arena(dsns, signing_key, url, a, b)


def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(t for t in after if before.get(t) != after[t])


def text_of(result: dict[str, Any]) -> str:
    texts = [c.get("text", "") for c in result.get("content", [])]
    return "\n".join(texts) + json.dumps(result.get("structuredContent"))


def code_of(result: dict[str, Any]) -> str | None:
    structured = result.get("structuredContent") or {}
    error = structured.get("error") if isinstance(structured, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def leaked(arena: Arena, out: str, sent: dict[str, Any]) -> set[str]:
    """The victim's names and ids in an answer, other than those the caller sent."""
    given = json.dumps(sent)
    return {ref for ref in arena.b.refs() if ref in out and ref not in given}


# ── 1. every route the spec marks refuses an agent session ──────────────────


def test_every_marked_route_is_known() -> None:
    assert set(MARKED) == set(KNOWN)


def test_every_other_write_is_listed() -> None:
    assert adversarial.unmarked_writes(SPEC) == adversarial.AGENT_MAY.keys()


@pytest.mark.parametrize("key", MARKED)
def test_refused_in_an_agent_session(arena: Arena, key: str) -> None:
    probe = KNOWN[key](arena.a.ids)
    before = arena.snapshot()
    with arena.v1(arena.a.api.access) as c:
        got = got_of(send(c, probe))
    assert got == problem(probe.expected)
    assert changed(before, arena.snapshot()) == []


def test_an_operator_agent_is_refused_a_decision(arena: Arena) -> None:
    probe = KNOWN["POST /v1/approvals/{approval_id}/decision"](arena.a.ids)
    before = arena.snapshot()
    with arena.v1(arena.a.operator_agent) as c:
        assert got_of(send(c, probe)) == problem("AGENT_SESSION_REFUSED")
    assert changed(before, arena.snapshot()) == []


@pytest.mark.parametrize("key", MARKED)
def test_the_person_gets_past_the_agent_check(arena: Arena, key: str) -> None:
    probe = KNOWN[key](arena.a.ids)
    with arena.v1(arena.a.human) as c:
        assert got_of(send(c, probe)) != problem("AGENT_SESSION_REFUSED")


# ── 2. the victim's refs through every tool ─────────────────────────────────

FOREIGN: dict[str, Callable[[Arena], tuple[str, dict[str, Any]]]] = {
    "get_app-victim-id": lambda w: ("get_app", {"app": w.b.app["id"]}),
    "get_status-victim-id": lambda w: ("get_status", {"app": w.b.app["id"]}),
    "get_status-victim-operation": lambda w: (
        "get_status",
        {"app": w.a.app["slug"], "operation": w.b.operation},
    ),
    "list_releases-victim-id": lambda w: ("list_releases", {"app": w.b.app["id"]}),
    "rollback-victim-app": lambda w: (
        "rollback",
        {"app": w.b.app["id"], "release": w.b.release, "env": "prod"},
    ),
    "rollback-victim-release-into-own-app": lambda w: (
        "rollback",
        {"app": w.a.app["slug"], "release": w.b.release, "env": "preview"},
    ),
    "deploy-victim-app": lambda w: ("deploy", {"app": w.b.app["id"]}),
    "request_share-victim-app": lambda w: (
        "request_share",
        {"app": w.b.app["id"], "env": "prod", "who": "org"},
    ),
    "request_connection-victim-app": lambda w: (
        "request_connection",
        {"app": w.b.app["id"], "connection": w.b.connection},
    ),
    "describe_connection-victim-app": lambda w: (
        "describe_connection",
        {"app": w.b.app["id"], "environment": "prod", "connection": w.b.connection},
    ),
    "get_logs-victim-app": lambda w: ("get_logs", {"app": w.b.app["id"], "env": "prod"}),
    "set_secret-victim-app": lambda w: (
        "set_secret",
        {"app": w.b.app["id"], "env": "preview", "name": w.b.secret},
    ),
}


@pytest.mark.parametrize("case", sorted(FOREIGN))
def test_the_victims_refs_are_not_found(arena: Arena, case: str) -> None:
    name, args = FOREIGN[case](arena)
    before = arena.snapshot()
    result = arena.rpc(name, args)
    assert result["isError"] is True, result
    assert code_of(result) in NOT_THERE
    assert leaked(arena, text_of(result), args) == set()
    assert changed(before, arena.snapshot()) == []


# The answer for the victim's name is the answer for a name that exists nowhere.
ORACLE: dict[str, Callable[[Arena, bool], tuple[str, dict[str, Any]]]] = {
    "get_app-victim-slug": lambda w, v: (
        "get_app",
        {"app": w.b.app["slug"] if v else "never-was-an-app"},
    ),
    "describe_connection-victim-connection": lambda w, v: (
        "describe_connection",
        {
            "app": w.a.app["slug"],
            "environment": "prod",
            "connection": w.b.connection if v else "never-was-a-connection",
        },
    ),
    "request_connection-victim-connection": lambda w, v: (
        "request_connection",
        {"app": w.a.app["slug"], "connection": w.b.connection if v else "never-was-a-connection"},
    ),
    "set_secret-victim-secret-name": lambda w, v: (
        "set_secret",
        {"app": w.a.app["slug"], "env": "preview", "name": w.b.secret if v else "NEVER_WAS"},
    ),
    "request_share-victim-user": lambda w, v: (
        "request_share",
        {
            "app": w.a.app["slug"],
            "env": "prod",
            "who": w.b.org.admin_user_id if v else f"usr_{ZERO}",
        },
    ),
    "create_app-victim-slug": lambda w, v: (
        "create_app",
        {"slug": w.b.app["slug"] if v else "never-was-an-app"},
    ),
}


def outcome(result: dict[str, Any]) -> tuple[bool, str | None]:
    return bool(result.get("isError")), code_of(result)


@pytest.mark.parametrize("case", sorted(ORACLE))
def test_the_victims_names_are_no_oracle(arena: Arena, case: str) -> None:
    name, args = ORACLE[case](arena, True)
    theirs = arena.rpc(name, args)
    other_name, other_args = ORACLE[case](arena, False)
    nowhere = arena.rpc(other_name, other_args)
    assert outcome(theirs) == outcome(nowhere)
    assert leaked(arena, text_of(theirs), args) == set()


@pytest.fixture(scope="module")
def victim_group(arena: Arena) -> str:
    group = new_id("grp")
    with psycopg.connect(arena.dsns.superuser) as conn:
        conn.execute(
            "insert into ssc.user_group (id, org_id, directory_ref, display_name) "
            "values (%s, %s, %s, 'Finance')",
            (group, arena.b.org.org_id, f"ref-{group}"),
        )
    return group


@pytest.mark.parametrize("kind", ["user", "group"])
def test_a_share_for_a_victims_subject_is_not_found(
    arena: Arena, victim_group: str, kind: str
) -> None:
    theirs = arena.b.org.admin_user_id if kind == "user" else victim_group
    results = []
    for who in (theirs, f"{theirs[:4]}{ZERO}"):
        before = arena.snapshot()
        result = arena.rpc("request_share", {"app": arena.a.app["slug"], "env": "prod", "who": who})
        assert changed(before, arena.snapshot()) == []
        results.append(outcome(result))
        assert leaked(arena, text_of(result), {"who": who}) == set()
    assert results == [(True, "REFERENCE_NOT_FOUND"), (True, "REFERENCE_NOT_FOUND")]


def test_grants_naming_a_victims_user_open_no_request(arena: Arena) -> None:
    env = env_id(arena.a.app, "prod")
    path = f"/v1/apps/{arena.a.app['id']}/environments/{env}/grants"
    with arena.v1(arena.a.api.access) as c:
        etag = c.get(path).headers["ETag"]
        before = arena.snapshot()
        grant = {"role": "user", "subject_kind": "user", "subject_id": arena.b.org.admin_user_id}
        r = c.put(path, json={"grants": [grant]}, headers={"If-Match": etag})
    assert got_of(r) == problem("REFERENCE_NOT_FOUND")
    assert changed(before, arena.snapshot()) == []


def test_a_victims_bundle_digest_is_not_reused(arena: Arena) -> None:
    args = {"app": arena.a.app["slug"], "bundle_digest": arena.b.digest, "size_bytes": 13}
    result = arena.rpc("deploy", args)
    assert result["isError"] is False, result
    assert result["structuredContent"]["stage"] == "upload"
    assert leaked(arena, text_of(result), args) == set()


@pytest.mark.parametrize(
    "tool", ["get_platform_requirements", "get_org_deployment_policy", "preflight"]
)
def test_org_wide_tools_say_nothing_of_the_victim(arena: Arena, tool: str) -> None:
    result = arena.rpc(tool, {})
    assert result["isError"] is False, result
    assert leaked(arena, text_of(result), {}) == set()


def test_list_apps_and_connections_show_only_the_attackers(arena: Arena) -> None:
    org = arena.a.org.org_id
    ids = {
        r[0] for r in rows(arena.dsns.app, org, "select id from ssc.app where org_id = %s", (org,))
    }
    apps = arena.rpc("list_apps", {})
    assert {a["id"] for a in apps["structuredContent"]["apps"]} <= ids
    connections = arena.rpc("list_connections", {})
    names = {c["name"] for c in connections["structuredContent"]["connections"]}
    assert names == {arena.a.connection}
    for result in (apps, connections):
        assert leaked(arena, text_of(result), {}) == set()


def test_there_is_no_tool_to_approve(arena: Arena) -> None:
    r = mcp_post(arena.url, arena.a.mcp.access, rpc_call("approve", {"id": arena.a.own_approval}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" in body or body["result"]["isError"] is True
    assert not any("approv" in t or "decide" in t for t in TOOLS)


# ── 3. hostile arguments ─────────────────────────────────────────────────────

MIB = "a" * 2**20
HOSTILE: dict[str, Any] = {
    "one-mib": MIB,
    "control-chars": "adv\x00\r\nX-Injected: 1\x1b[31m",
    "dot-dot": "../../internal/v1/heartbeat",
    "home": "${HOME}",
    "command": "$(id)",
    "json-object": {"$ne": None},
    "sixty-four-a": "a" * 64,
    "trailing-dot": "attacker-app.",
}
ECHOED = {
    "one-mib": "a" * 64,
    "control-chars": "X-Injected",
    "dot-dot": "internal/v1/heartbeat",
    "home": "${HOME}",
    "command": "$(id)",
    "sixty-four-a": "a" * 64,
    "trailing-dot": "attacker-app.",
}


def base_args(w: Arena) -> dict[str, dict[str, Any]]:
    slug = w.a.app["slug"]
    return {
        "create_app": {"slug": "hostile-app", "idempotency_key": new_key()},
        "get_app": {"app": slug},
        "get_status": {"app": slug},
        "list_releases": {"app": slug},
        "rollback": {"app": slug, "release": w.b.release, "env": "preview"},
        "deploy": {"app": slug},
        "request_share": {"app": slug, "env": "prod", "who": "org", "role": "user"},
        "request_connection": {"app": slug, "connection": w.a.connection},
        "describe_connection": {"app": slug, "environment": "prod", "connection": w.a.connection},
        "get_logs": {"app": slug, "env": "preview"},
        "set_secret": {"app": slug, "env": "preview", "name": "HOSTILE_NAME"},
    }


STRING_ARGS = {
    "create_app": ("slug", "idempotency_key"),
    "get_app": ("app",),
    "get_status": ("app", "operation", "build"),
    "list_releases": ("app",),
    "rollback": ("app", "release", "env", "idempotency_key"),
    "deploy": ("app", "bundle_digest", "idempotency_key"),
    "request_share": ("app", "env", "who", "role", "idempotency_key"),
    "request_connection": ("app", "connection", "idempotency_key"),
    "describe_connection": ("app", "environment", "connection"),
    "get_logs": ("app", "env", "source", "after"),
    "set_secret": ("app", "env", "name"),
}
INJECTIONS = [
    pytest.param(tool, arg, payload, id=f"{tool}.{arg}-{payload}")
    for tool, args in STRING_ARGS.items()
    for arg in args
    for payload in HOSTILE
]


@pytest.mark.parametrize(("tool", "arg", "payload"), INJECTIONS)
def test_hostile_arguments_are_refused_and_never_echoed(
    arena: Arena, caplog: pytest.LogCaptureFixture, tool: str, arg: str, payload: str
) -> None:
    args = {**base_args(arena)[tool], arg: HOSTILE[payload]}
    with caplog.at_level(logging.DEBUG):
        r = mcp_post(arena.url, arena.a.mcp.access, rpc_call(tool, args))
    if payload == "one-mib":
        assert r.status_code == 413, r.text[:200]
    else:
        assert r.status_code == 200, r.text[:200]
        result = r.json()["result"]
        assert code_of(result) not in ("BAD_RESPONSE", "INTERNAL"), result
        if arg != "idempotency_key" or payload in ("control-chars", "json-object"):
            assert result["isError"] is True, result
    if payload in ECHOED:
        assert ECHOED[payload] not in caplog.text


V1_INJECTIONS = [
    pytest.param(route, payload, id=f"{route}-{payload}")
    for route in ("create-app.slug", "approval.subject_key", "app-path")
    for payload in HOSTILE
    if not (route == "app-path" and payload in ("one-mib", "json-object", "control-chars"))
]


def v1_hostile(w: Arena, route: str, value: Any) -> httpx2.Response:
    token = w.a.api.access
    if route == "create-app.slug":
        return call(w.url, token, "POST", "/v1/apps", {"slug": value})
    if route == "approval.subject_key":
        ask = {"environment_id": env_id(w.a.app, "preview"), "kind": "connect_data_source"}
        return call(w.url, token, "POST", "/v1/approvals", {**ask, "subject_key": value})
    return httpx2.get(f"{w.url}/v1/apps/{value}", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize(("route", "payload"), V1_INJECTIONS)
def test_hostile_v1_input_is_refused_and_never_echoed(
    arena: Arena, caplog: pytest.LogCaptureFixture, route: str, payload: str
) -> None:
    before = arena.snapshot()
    with caplog.at_level(logging.DEBUG):
        r = v1_hostile(arena, route, HOSTILE[payload])
    assert 400 <= r.status_code < 500, r.text[:300]
    assert changed(before, arena.snapshot()) == []
    if payload in ECHOED and route != "app-path":
        assert ECHOED[payload] not in caplog.text


@pytest.mark.parametrize("tool", sorted(t for t, a in STRING_ARGS.items() if "app" in a))
def test_another_orgs_id_is_not_found_and_logged_as_a_refusal(
    arena: Arena, caplog: pytest.LogCaptureFixture, tool: str
) -> None:
    with caplog.at_level(logging.INFO):
        result = arena.rpc(tool, {**base_args(arena)[tool], "app": arena.b.app["id"]})
    assert code_of(result) == "NOT_FOUND", result
    assert "refusal" in caplog.text


# ── 4. audiences and credentials ─────────────────────────────────────────────


def v1_status(url: str, token: str, path: str = "/v1/apps") -> str:
    return got_of(httpx2.get(f"{url}{path}", headers={"Authorization": f"Bearer {token}"}))


def mcp_challenge(url: str, token: str) -> str:
    r = mcp_post(url, token, rpc_call("list_apps", {}))
    meta = f'resource_metadata="{url}/.well-known/oauth-protected-resource/mcp"'
    return (
        "401 resource_metadata"
        if r.status_code == 401 and meta in r.headers.get("WWW-Authenticate", "")
        else got_of(r)
    )


AUDIENCES: dict[str, Callable[[Arena], tuple[str, str]]] = {
    "mcp-token-on-v1": lambda w: (v1_status(w.url, w.a.mcp.access), problem("UNAUTHENTICATED")),
    "api-agent-token-on-mcp": lambda w: (
        mcp_challenge(w.url, w.a.api.access),
        "401 resource_metadata",
    ),
    "preview-ci-token-on-mcp": lambda w: (mcp_challenge(w.url, w.a.ci), "401 resource_metadata"),
    "refresh-token-on-v1": lambda w: (
        v1_status(w.url, w.a.api.refresh),
        problem("UNAUTHENTICATED"),
    ),
    "refresh-token-on-mcp": lambda w: (
        mcp_challenge(w.url, w.a.mcp.refresh),
        "401 resource_metadata",
    ),
    "victim-app-id-on-v1": lambda w: (
        v1_status(w.url, w.a.api.access, f"/v1/apps/{w.b.app['id']}"),
        problem("NOT_FOUND"),
    ),
    "victim-approval-on-v1": lambda w: (
        v1_status(w.url, w.a.api.access, f"/v1/approvals/{w.b.approval}"),
        problem("NOT_FOUND"),
    ),
    "victim-operation-on-v1": lambda w: (
        v1_status(w.url, w.a.api.access, f"/v1/operations/{w.b.operation}"),
        problem("NOT_FOUND"),
    ),
}


@pytest.mark.parametrize("case", sorted(AUDIENCES))
def test_audience_and_credential_confusion(arena: Arena, case: str) -> None:
    got, expected = AUDIENCES[case](arena)
    assert got == expected


async def revoke(dsns: Dsns, refresh: str) -> None:
    """The auth host's ``/revoke``, as ``ssc logout`` calls it."""
    s = auth_settings(dsns)
    engine = make_engine(dsns.app)
    workos = FakeWorkOS().client()
    signer = tokens.Signer(s.signing_pem, s.signing_kid, s.auth_url)
    try:
        async with client_for(AuthHost(s, engine, workos, signer, DevCallers(SECRET))) as http:
            r = await http.post("/revoke", data={"token": refresh})
        assert r.status_code == 200, r.text
    finally:
        await workos.aclose()
        await engine.dispose()


def test_a_revoked_login_is_refused(arena: Arena) -> None:
    org, admin = arena.a.org.org_id, arena.a.org.admin_user_id
    api = login(arena.dsns, arena.key, org, admin, audience=USER_AUDIENCE, agent=CLIENT_ID)
    mcp = login(arena.dsns, arena.key, org, admin, audience=f"{arena.url}/mcp", agent=CLIENT_ID)
    assert v1_status(arena.url, api.access) == "200"
    assert mcp_post(arena.url, mcp.access, rpc_call("list_apps", {})).status_code == 200
    asyncio.run(revoke(arena.dsns, api.refresh))
    asyncio.run(revoke(arena.dsns, mcp.refresh))
    assert v1_status(arena.url, api.access) == problem("UNAUTHENTICATED")
    assert mcp_challenge(arena.url, mcp.access) == "401 resource_metadata"


# ── 5. secrets ───────────────────────────────────────────────────────────────


def test_set_secret_takes_no_value(arena: Arena) -> None:
    r = mcp_post(arena.url, arena.a.mcp.access, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    schema = next(t for t in r.json()["result"]["tools"] if t["name"] == "set_secret")
    assert set(schema["inputSchema"]["properties"]) == {"app", "env", "name"}
    before = arena.snapshot()
    args = {"app": arena.a.app["slug"], "env": "preview", "name": "STRIPE_KEY", "value": "sk_x"}
    result = arena.rpc("set_secret", args)
    assert "sk_x" not in text_of(result)
    assert changed(before, arena.snapshot()) == []


@pytest.mark.parametrize("shape", sorted(SHAPED))
def test_get_logs_masks_every_credential_shape(arena: Arena, shape: str) -> None:
    result = arena.rpc("get_logs", {"app": arena.a.app["slug"], "env": "preview"})
    assert result["isError"] is False, result
    assert SHAPED[shape] not in text_of(result)


@pytest.mark.parametrize(
    "tool",
    [
        ("get_app", lambda w: {"app": w.a.app["slug"]}),
        ("get_status", lambda w: {"app": w.a.app["slug"]}),
        (
            "describe_connection",
            lambda w: {"app": w.a.app["slug"], "environment": "prod", "connection": w.a.connection},
        ),
        ("list_connections", lambda w: {}),
    ],
    ids=lambda t: t[0],
)
def test_no_address_proxy_or_connection_value(
    arena: Arena, tool: tuple[str, Callable[[Arena], dict[str, Any]]]
) -> None:
    name, args = tool
    out = text_of(arena.rpc(name, args(arena)))
    for value in (PLANTED_HOST, PLANTED_DATABASE, "postgres://", "postgresql://"):
        assert value not in out
    assert adversarial.LEAKY.search(out) is None


# ── 6. approvals stay with people ───────────────────────────────────────────

APPROVALS: dict[str, Callable[[Arena], tuple[str, str, dict[str, Any] | None, str]]] = {
    "approve-own-request": lambda w: (
        "POST",
        f"/v1/approvals/{w.a.own_approval}/decide",
        {"outcome": "approved", "reason": "mine"},
        problem("AGENT_SESSION_REFUSED"),
    ),
    "decide-own-request": lambda w: (
        "POST",
        f"/v1/approvals/{w.a.own_approval}/decision",
        {
            "outcome": "approved",
            "approver_user_id": w.a.org.admin_user_id,
            "channel": "chat",
            "reason": "mine",
        },
        problem("FORBIDDEN"),
    ),
    "approve-someone-elses-request": lambda w: (
        "POST",
        f"/v1/approvals/{w.a.others_approval}/decide",
        {"outcome": "approved", "reason": "theirs"},
        problem("AGENT_SESSION_REFUSED"),
    ),
    "withdraw-someone-elses-request": lambda w: (
        "POST",
        f"/v1/approvals/{w.a.others_approval}/cancel",
        {"reason": "theirs", "channel": "cli"},
        problem("FORBIDDEN"),
    ),
    "approve-victims-request": lambda w: (
        "POST",
        f"/v1/approvals/{w.b.approval}/decide",
        {"outcome": "approved", "reason": "theirs"},
        problem("AGENT_SESSION_REFUSED"),
    ),
    "withdraw-victims-request": lambda w: (
        "POST",
        f"/v1/approvals/{w.b.approval}/cancel",
        {"reason": "theirs", "channel": "cli"},
        problem("NOT_FOUND"),
    ),
}
NAMED_ROUTES = {
    "promote": "POST /v1/apps/{app_id}/promote",
    "warm": "PUT /v1/warm",
    "repo-link": "PUT /v1/apps/{app_id}/github",
    "egress": "PUT /v1/egress/hosts/{host}",
    "create-connection": "POST /v1/connections",
    "delete-app": "POST /v1/apps/{app_id}/delete",
    "link-login": "POST /v1/unlinked-logins/{unlinked_login_id}/link",
}
for _case, _key in NAMED_ROUTES.items():
    APPROVALS[_case] = lambda w, k=_key: (
        KNOWN[k](w.a.ids).method,
        KNOWN[k](w.a.ids).path,
        KNOWN[k](w.a.ids).body,
        problem("AGENT_SESSION_REFUSED"),
    )


@pytest.mark.parametrize("case", sorted(APPROVALS))
def test_approval_paths(arena: Arena, case: str) -> None:
    method, path, body, expected = APPROVALS[case](arena)
    before = arena.snapshot()
    assert got_of(call(arena.url, arena.a.api.access, method, path, body)) == expected
    assert changed(before, arena.snapshot()) == []


def test_an_agent_withdraws_its_own_request(arena: Arena) -> None:
    """Decision 016: an agent session may withdraw a request it opened, and nothing else."""
    preview = env_id(arena.a.app, "preview")
    ask = {"environment_id": preview, "kind": "connect_data_source", "subject_key": "withdrawn"}
    asked = made(call(arena.url, arena.a.api.access, "POST", "/v1/approvals", ask))
    path = f"/v1/approvals/{asked['id']}/cancel"
    r = call(arena.url, arena.a.api.access, "POST", path, {"reason": "mine", "channel": "cli"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "cancelled"


# ── 7. rate limits ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def limited(arena: Arena) -> Iterator[str]:
    with serving(settings(arena.dsns, arena.key, 60, 0.2)) as url:
        yield url


def logins(arena: Arena, url: str) -> tuple[str, str]:
    org, admin = arena.a.org.org_id, arena.a.org.admin_user_id
    mcp = f"{url}/mcp"
    one = login(arena.dsns, arena.key, org, admin, audience=mcp, agent=CLIENT_ID)
    two = login(arena.dsns, arena.key, org, admin, audience=mcp, agent=CLIENT_ID)
    return one.access, two.access


def test_the_61st_list_apps_is_rate_limited_per_credential(arena: Arena, limited: str) -> None:
    one, two = logins(arena, limited)
    answers = [mcp_post(limited, one, rpc_call("list_apps", {})) for _ in range(61)]
    results = [a.json()["result"] for a in answers]
    assert [r["isError"] for r in results] == [False] * 60 + [True]
    assert code_of(results[60]) == "RATE_LIMITED"
    other = mcp_post(limited, two, rpc_call("list_apps", {})).json()["result"]
    assert other["isError"] is False
    time.sleep(5.5)
    again = mcp_post(limited, one, rpc_call("list_apps", {})).json()["result"]
    assert again["isError"] is False


def test_v1_rate_limit_says_when_to_retry(arena: Arena, limited: str) -> None:
    org, admin = arena.a.org.org_id, arena.a.org.admin_user_id
    api = login(arena.dsns, arena.key, org, admin, audience=USER_AUDIENCE, agent=CLIENT_ID)
    answers = [
        httpx2.get(f"{limited}/v1/apps", headers={"Authorization": f"Bearer {api.access}"})
        for _ in range(61)
    ]
    assert [a.status_code for a in answers] == [200] * 60 + [429]
    assert got_of(answers[60]) == problem("RATE_LIMITED")
    wait = int(answers[60].headers["Retry-After"])
    time.sleep(wait + 0.5)
    assert v1_status(limited, api.access) == "200"


def test_mcp_rate_limit_says_when_to_retry(arena: Arena, limited: str) -> None:
    one, _ = logins(arena, limited)
    for _ in range(60):
        mcp_post(limited, one, rpc_call("list_apps", {}))
    result = mcp_post(limited, one, rpc_call("list_apps", {})).json()["result"]
    error = result["structuredContent"]["error"]
    assert error["code"] == "RATE_LIMITED"
    wait = error["retry_after"]
    assert isinstance(wait, int)
    assert wait >= 1
    assert f"Retry after {wait} seconds." in result["content"][0]["text"]
    time.sleep(wait + 0.5)
    assert mcp_post(limited, one, rpc_call("list_apps", {})).json()["result"]["isError"] is False


def test_a_refusal_without_a_wait_says_none(arena: Arena) -> None:
    error = arena.rpc("get_app", {"app": f"app_{ZERO}"})["structuredContent"]["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["retry_after"] is None


# ── 8. idempotency ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def idem_app(arena: Arena) -> tuple[dict[str, Any], tuple[str, str]]:
    app = made(call(arena.url, arena.a.human, "POST", "/v1/apps", {"slug": "idem-app"}))
    org, admin = arena.a.org.org_id, arena.a.org.admin_user_id
    return app, (
        add_release(arena.dsns.app, org, app["id"], 1, admin),
        add_release(arena.dsns.app, org, app["id"], 2, admin),
    )


def deployments(
    arena: Arena, app: dict[str, Any], env: str, key: str, release: str
) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {arena.a.api.access}", "Idempotency-Key": key}
    path = f"{arena.url}/v1/apps/{app['id']}/environments/{env_id(app, env)}/deployments"
    return httpx2.post(path, json={"release_id": release, "kind": "rollback"}, headers=headers)


def test_the_same_key_with_another_deploy_body_is_refused(
    arena: Arena, idem_app: tuple[dict[str, Any], tuple[str, str]]
) -> None:
    app, (one, two) = idem_app
    key = new_key()
    first = deployments(arena, app, "prod", key, one)
    assert first.status_code == 202, first.text
    assert got_of(deployments(arena, app, "prod", key, two)) == problem("IDEMPOTENCY_KEY_REUSED")
    again = deployments(arena, app, "prod", key, one)
    assert again.json()["operation_id"] == first.json()["operation_id"]
    audit = rows(
        arena.dsns.app,
        arena.a.org.org_id,
        "select action from ssc.audit_event where target_id = %s",
        (first.json()["operation_id"],),
    )
    assert audit == [("rollback.started",)]


def test_a_tool_retry_with_the_same_key_and_another_release_is_refused(
    arena: Arena, idem_app: tuple[dict[str, Any], tuple[str, str]]
) -> None:
    app, (one, two) = idem_app
    args = {"app": app["slug"], "release": one, "env": "preview", "idempotency_key": new_key()}
    first = arena.rpc("rollback", args)
    assert first["isError"] is False, first
    assert code_of(arena.rpc("rollback", {**args, "release": two})) == "IDEMPOTENCY_KEY_REUSED"
    again = arena.rpc("rollback", args)
    op = first["structuredContent"]["operation_id"]
    assert again["structuredContent"]["operation_id"] == op
    audit = rows(
        arena.dsns.app,
        arena.a.org.org_id,
        "select action, actor_kind, actor_id, actor_via_agent, actor_client_id "
        "from ssc.audit_event where target_id = %s",
        (op,),
    )
    assert audit == [("rollback.started", "user", arena.a.org.admin_user_id, True, CLIENT_ID)]


# ── 9. audit ─────────────────────────────────────────────────────────────────


def test_agent_writes_are_audited_as_the_person_via_the_agent(arena: Arena) -> None:
    created = arena.rpc("create_app", {"slug": "audited-app"})
    assert created["isError"] is False, created
    targets = (created["structuredContent"]["app"]["id"], arena.a.own_approval)
    audit = rows(
        arena.dsns.app,
        arena.a.org.org_id,
        "select action, actor_kind, actor_id, actor_via_agent, actor_client_id "
        "from ssc.audit_event where target_id = any(%s) order by action",
        (list(targets),),
    )
    who = ("user", arena.a.org.admin_user_id, True, CLIENT_ID)
    assert audit == [("app.created", *who), ("approval.requested", *who)]


def test_refused_tool_calls_leave_no_row(arena: Arena) -> None:
    before = arena.snapshot()
    for name, args in (
        ("rollback", {"app": arena.a.app["slug"], "release": arena.b.release, "env": "prod"}),
        ("request_share", {"app": arena.b.app["id"], "env": "prod", "who": "org"}),
        ("set_secret", {"app": arena.a.app["slug"], "env": "prod", "name": "SSC_TOKEN"}),
    ):
        assert arena.rpc(name, args)["isError"] is True
    assert changed(before, arena.snapshot()) == []
