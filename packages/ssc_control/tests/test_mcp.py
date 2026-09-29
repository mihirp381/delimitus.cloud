"""SSC-048: the agent interface, through an MCP client over streamable HTTP against the real API
served by uvicorn on a free port, backed by postgres:18.

Ticket "done when" checks:
  * a folder deploys through the interface and the  -> test_deploy_to_preview (stages pack,
    agent receives an address                           upload, building, deploying, live, with
                                                        a preview-scoped credential; deploying
                                                        and live carry preview's url)
  * a preview-scoped agent never touches prod        -> test_preview_scoped_agent_asks_nothing,
                                     test_preview_scoped_agent_cannot_roll_back_prod
  * a share request from an agent is pending and cannot be approved from the same session
                                                     -> test_request_share_pending,
                                                        test_no_approve_path
  * every call records agent and client id           -> test_mutations_audited_as_agent,
                                                        test_deploy_to_preview,
                                                        test_request_share_pending
Plus: only agent credentials get in, the tool set is the allowlist, both protocol eras work,
refusals are tool errors carrying the problem, rate limits are per credential, and the OpenAPI
file is unchanged. Deferred: local `ssc mcp` (lane C), logs (SSC-024).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import socket
import tarfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from ssc_testkit import ISSUER, Dsns, SigningKey, make_org, mint, new_key

from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.api.mcp import tools
from ssc_control.api.mcp.tools import TOOLS
from ssc_control.api.openapi import build_spec, spec_json
from ssc_control.api.settings import INTERNAL_AUDIENCE, USER_AUDIENCE
from ssc_control.db import CreatedOrg, bind_org_sync, make_engine
from ssc_control.deploy.build_driver import FakeBuildDriver
from ssc_control.deploy.builds import run_build
from ssc_control.worker_ports import Ports
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock

SPEC = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"
ALLOWLIST = {
    "list_apps",
    "get_app",
    "get_status",
    "list_releases",
    "rollback",
    "deploy",
    "request_share",
    "request_connection",
}
CLIENT_ID = "claude-code"
JSONRPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-11-25",
}

# ── database helpers ─────────────────────────────────────────────────────────


def rows(dsn: str, org: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        return conn.execute(sql, params).fetchall()


def add_release(dsn: str, org: str, app: str) -> str:
    rid = new_id("rel")
    d = "sha256:" + hashlib.sha256(rid.encode()).hexdigest()
    rows(
        dsn,
        org,
        "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
        "source_digest, actor_kind, actor_id) values (%s, %s, %s, 1, %s, %s, %s, 'user', %s) "
        "returning id",
        (rid, org, app, d, d, d, new_id("usr")),
    )
    return rid


def add_account(dsn: str, org: str, role: str) -> str:
    uid = new_id("usr")
    rows(
        dsn,
        org,
        "insert into ssc.user_account (id, org_id, display_name, email, role, status) "
        "values (%s, %s, 'Some One', 'someone@example.com', %s, 'active') returning id",
        (uid, org, role),
    )
    return uid


def add_grant(dsn: str, org: str, env: str, user: str, role: str, by: str) -> None:
    rows(
        dsn,
        org,
        "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, "
        "granted_by_user_id) values (%s, %s, %s, %s, 'user', %s, %s) returning id",
        (new_id("gnt"), org, env, role, user, by),
    )


def tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), mtime=0)


# ── the live server ──────────────────────────────────────────────────────────


@contextmanager
def serving(settings_for: Callable[[str], Settings], blobs: Path | None = None) -> Iterator[str]:
    """``create_app`` under uvicorn on a free port, in a thread; yields its base URL. With
    ``blobs``, bundles go to a filesystem store there, served at ``<url>/blobs``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    store = None
    if blobs is not None:
        signer = UrlSigner({"k1": b"k" * 32}, active="k1", clock=SystemClock())
        store = FsBlobStore(blobs, signer=signer, base_url=f"{url}/blobs")
    app = create_app(settings_for(url), None, store)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive(), "uvicorn stopped while starting"
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()


def settings(
    dsns: Dsns, key: SigningKey, capacity: int, refill: float
) -> Callable[[str], Settings]:
    def build(url: str) -> Settings:
        return Settings(
            database_dsn=dsns.app,
            jwks={"keys": [key.jwk]},
            issuer=ISSUER,
            rate_capacity=capacity,
            rate_refill_per_second=refill,
            public_url=url,
            environment="test",
        )

    return build


@dataclass(frozen=True)
class World:
    dsns: Dsns
    key: SigningKey
    url: str
    org: CreatedOrg
    app: dict[str, Any]
    release: str
    human: str
    agent: str

    def agent_token(self, **claims: Any) -> str:
        base: dict[str, Any] = {"agent": True, "client_id": CLIENT_ID}
        return mint(self.key, org=self.org.org_id, sub=self.org.admin_user_id, **(base | claims))


@pytest.fixture(scope="module")
def world(
    dsns: Dsns, signing_key: SigningKey, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    org = make_org(dsns.app, "MCP org")
    human = mint(signing_key, org=org.org_id, sub=org.admin_user_id)
    agent = mint(
        signing_key, org=org.org_id, sub=org.admin_user_id, agent=True, client_id=CLIENT_ID
    )
    blobs = tmp_path_factory.mktemp("blobs")
    with serving(settings(dsns, signing_key, 1000, 1000.0), blobs) as url:
        r = httpx2.post(
            f"{url}/v1/apps",
            json={"slug": "mcp-app"},
            headers={"Authorization": f"Bearer {human}", "Idempotency-Key": new_key()},
        )
        assert r.status_code == 201, r.text
        app = r.json()
        release = add_release(dsns.app, org.org_id, app["id"])
        yield World(dsns, signing_key, url, org, app, release, human, agent)


@asynccontextmanager
async def session(url: str, token: str, mode: str = "auto") -> AsyncIterator[Client]:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http,
        Client(
            streamable_http_client(f"{url}/mcp", http_client=http), cache=None, mode=mode
        ) as client,
    ):
        yield client


def env_id(app: dict[str, Any], name: str) -> str:
    return next(e["id"] for e in app["environments"] if e["name"] == name)


def rpc_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


# ── tool surface ─────────────────────────────────────────────────────────────


async def test_tool_set_is_the_allowlist(world: World) -> None:
    async with session(world.url, world.agent) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == ALLOWLIST == set(TOOLS)
    for name in ("list_apps", "get_app", "get_status", "list_releases"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is True
    for name in ("rollback", "deploy", "request_share", "request_connection"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is False
    for name in ("rollback", "deploy"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.destructive_hint is True


@pytest.mark.parametrize("mode", ["auto", "2026-07-28", "legacy"])
async def test_read_tools(world: World, mode: str) -> None:
    async with session(world.url, world.agent, mode) as client:
        listed = await client.call_tool("list_apps", {})
        by_slug = await client.call_tool("get_app", {"app": "mcp-app"})
        by_id = await client.call_tool("get_app", {"app": world.app["id"]})
        status = await client.call_tool("get_status", {"app": "mcp-app"})
    assert not listed.is_error
    assert [a["slug"] for a in listed.structured_content["apps"]] == ["mcp-app"]
    assert json.loads(listed.content[0].text) == listed.structured_content
    assert by_slug.structured_content == by_id.structured_content
    assert by_id.structured_content["id"] == world.app["id"]
    assert {e["name"] for e in by_id.structured_content["environments"]} == {"prod", "preview"}
    assert status.structured_content["app"]["id"] == world.app["id"]
    assert status.structured_content["current"] == {"prod": None, "preview": None}


async def test_unknown_app_and_bad_arguments_are_tool_errors(world: World) -> None:
    async with session(world.url, world.agent) as client:
        missing = await client.call_tool("get_app", {"app": "no-such-app"})
        other = await client.call_tool("get_app", {"app": "app_" + "0" * 20})
        bad = await client.call_tool("get_app", {"app": "../internal/v1/heartbeat"})
    assert missing.is_error
    assert missing.structured_content["error"]["code"] == "APP_NOT_FOUND"
    assert missing.structured_content["error"]["status"] is None
    assert other.is_error
    assert other.structured_content["error"]["code"] == "NOT_FOUND"
    assert other.structured_content["error"]["status"] == 404
    assert bad.is_error
    assert bad.structured_content is None


# ── writes go through /v1 as the agent ───────────────────────────────────────


async def test_mutations_audited_as_agent(world: World) -> None:
    key = new_key()
    args = {"app": "mcp-app", "release": world.release, "env": "prod", "idempotency_key": key}
    async with session(world.url, world.agent) as client:
        first = await client.call_tool("rollback", args)
        again = await client.call_tool("rollback", args)
        second = await client.call_tool("rollback", {**args, "idempotency_key": new_key()})
        op = first.structured_content["operation_id"]
        status = await client.call_tool("get_status", {"app": "mcp-app", "operation": op})

    assert not first.is_error, first.content
    assert first.structured_content == {
        "operation_id": op,
        "state": "pending",
        "location": f"/v1/operations/{op}",
        "idempotency_key": key,
    }
    assert again.structured_content == first.structured_content
    assert second.is_error
    assert second.structured_content["error"]["code"] == "DEPLOYMENT_IN_FLIGHT"
    assert status.structured_content["operation"]["kind"] == "rollback"
    assert status.structured_content["operation"]["state"] == "pending"

    deployment = rows(
        world.dsns.app,
        world.org.org_id,
        "select kind, environment_id, actor_kind, actor_id, actor_via_agent, actor_client_id "
        "from ssc.deployment where id = %s",
        (op,),
    )
    assert deployment == [
        ("rollback", env_id(world.app, "prod"), "user", world.org.admin_user_id, True, CLIENT_ID)
    ]
    audit = rows(
        world.dsns.app,
        world.org.org_id,
        "select action, actor_kind, actor_id, actor_via_agent, actor_client_id "
        "from ssc.audit_event where target_id = %s",
        (op,),
    )
    assert audit == [("rollback.started", "user", world.org.admin_user_id, True, CLIENT_ID)]


async def test_preview_scoped_agent_cannot_roll_back_prod(world: World) -> None:
    args = {"app": "mcp-app", "release": world.release, "env": "prod", "idempotency_key": new_key()}
    async with session(world.url, world.agent_token(scope="preview")) as client:
        prod = await client.call_tool("rollback", args)
        apps = await client.call_tool("list_apps", {})
    assert prod.is_error
    assert prod.structured_content["error"]["code"] == "FORBIDDEN"
    assert not apps.is_error


async def test_preview_scoped_agent_asks_nothing(world: World) -> None:
    """``POST /v1/approvals`` names no environment in its path and is not a preview-scope change,
    so check_scope refuses every request tool, prod ones included."""
    member = add_account(world.dsns.app, world.org.org_id, "member")
    calls = [
        ("request_connection", {"app": "mcp-app", "connection": "scoped-db"}),
        ("request_share", {"app": "mcp-app", "env": "prod", "who": member}),
        ("request_share", {"app": "mcp-app", "env": "preview", "who": member}),
    ]
    async with session(world.url, world.agent_token(scope="preview")) as client:
        results = [await client.call_tool(name, args) for name, args in calls]
    for r in results:
        assert r.is_error
        assert r.structured_content["error"]["code"] == "FORBIDDEN"
    asked = rows(
        world.dsns.app,
        world.org.org_id,
        "select count(*) from ssc.approval_request where subject_key = %s or payload::text like %s",
        ("scoped-db", f"%{member}%"),
    )
    assert asked == [(0,)]


async def test_tool_error_carries_the_request_id(world: World) -> None:
    async with httpx2.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"{world.url}/mcp",
            json=rpc_call("get_app", {"app": "app_" + "1" * 20}),
            headers={"Authorization": f"Bearer {world.agent}", **JSONRPC_HEADERS},
        )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert set(error) == {"type", "title", "status", "detail", "instance", "code", "request_id"}
    assert error["code"] == "NOT_FOUND"
    assert error["request_id"] == r.headers["X-Request-Id"]
    assert error["request_id"] in result["content"][0]["text"]


async def test_rate_limit_per_credential(dsns: Dsns, signing_key: SigningKey, world: World) -> None:
    other = world.agent_token()
    with serving(settings(dsns, signing_key, 3, 0.001)) as url:
        async with session(url, world.agent) as client:
            answers = [await client.call_tool("list_apps", {}) for _ in range(4)]
        async with session(url, other) as client:
            fresh = await client.call_tool("list_apps", {})
    assert [a.is_error for a in answers] == [False, False, False, True]
    assert answers[3].structured_content["error"]["code"] == "RATE_LIMITED"
    assert not fresh.is_error


# ── only agent credentials get in ────────────────────────────────────────────


async def post_initialize(url: str, token: str | None) -> httpx2.Response:
    headers = dict(JSONRPC_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    async with httpx2.AsyncClient(timeout=30) as http:
        return await http.post(f"{url}/mcp", json=body, headers=headers)


def assert_refused(r: httpx2.Response, url: str) -> None:
    assert r.status_code == 401, r.text
    challenge = r.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{url}/.well-known/oauth-protected-resource/mcp"' in challenge


async def test_agent_token_is_accepted(world: World) -> None:
    r = await post_initialize(world.url, world.agent)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "ssc"


async def test_non_agent_token_401(world: World) -> None:
    assert_refused(await post_initialize(world.url, world.human), world.url)
    assert_refused(await post_initialize(world.url, None), world.url)
    assert_refused(await post_initialize(world.url, world.agent_token(agent=False)), world.url)
    claimed = mint(world.key, org=world.org.org_id, sub=world.org.admin_user_id, client_id="x")
    assert_refused(await post_initialize(world.url, claimed), world.url)
    for kind in ("workload", "operator"):
        assert_refused(await post_initialize(world.url, world.agent_token(kind=kind)), world.url)


async def test_missing_client_id_401(world: World) -> None:
    token = mint(world.key, org=world.org.org_id, sub=world.org.admin_user_id, agent=True)
    assert_refused(await post_initialize(world.url, token), world.url)


async def test_wrong_audience_401(world: World) -> None:
    for audience in (INTERNAL_AUDIENCE, f"{world.url}/mcp"):
        token = world.agent_token(audience=audience)
        assert_refused(await post_initialize(world.url, token), world.url)
    expired = world.agent_token(expires_in=-60)
    assert_refused(await post_initialize(world.url, expired), world.url)


async def test_protected_resource_metadata(world: World) -> None:
    async with httpx2.AsyncClient(timeout=30) as http:
        r = await http.get(f"{world.url}/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["resource"] == f"{world.url}/mcp"
    assert meta["authorization_servers"] == [ISSUER]
    assert meta["bearer_methods_supported"] == ["header"]


async def test_foreign_host_is_refused(world: World) -> None:
    async with httpx2.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"{world.url}/mcp",
            json=rpc_call("list_apps", {}),
            headers={
                "Authorization": f"Bearer {world.agent}",
                "Host": "rebind.example",
                **JSONRPC_HEADERS,
            },
        )
    assert r.status_code == 421


# ── approvals stay with people (decision 016) ────────────────────────────────


async def test_no_approve_path(world: World) -> None:
    async with session(world.url, world.agent) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert not any(word in n for n in names for word in ("approv", "decide", "promote", "grant"))

    def post(path: str, token: str, body: dict[str, Any]) -> httpx2.Response:
        return httpx2.post(
            f"{world.url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": new_key()},
        )

    asked = post(
        "/v1/approvals",
        world.agent,
        {
            "environment_id": env_id(world.app, "prod"),
            "kind": "connect_data_source",
            "subject_key": "warehouse",
        },
    )
    assert asked.status_code == 201, asked.text
    assert asked.json()["requested_via_agent"] is True
    decision = {
        "outcome": "approved",
        "approver_user_id": world.org.admin_user_id,
        "channel": "chat",
        "reason": "looks fine",
    }
    path = f"/v1/approvals/{asked.json()['id']}/decision"
    agent_operator = mint(
        world.key, org=world.org.org_id, sub="op_staff", kind="operator", agent=True
    )
    operator = mint(world.key, org=world.org.org_id, sub="op_staff", kind="operator")
    assert post(path, world.agent, decision).json()["code"] == "FORBIDDEN"
    assert post(path, agent_operator, decision).json()["code"] == "AGENT_SESSION_REFUSED"
    assert post(path, operator, decision).json()["code"] == "SELF_APPROVAL_REFUSED"
    state = rows(
        world.dsns.app,
        world.org.org_id,
        "select state from ssc.approval_request where id = %s",
        (asked.json()["id"],),
    )
    assert state == [("pending",)]

    approver = add_account(world.dsns.app, world.org.org_id, "admin")
    approved = post(path, operator, {**decision, "approver_user_id": approver})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    assert approved.json()["decided_by_user_id"] == approver
    assert approved.json()["requested_by_user_id"] == world.org.admin_user_id


# ── asking: sharing and connections only open approval requests ─────────────


def grants_of(world: World, env: str) -> dict[str, Any]:
    r = httpx2.get(
        f"{world.url}/v1/apps/{world.app['id']}/environments/{env}/grants",
        headers={"Authorization": f"Bearer {world.human}"},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_request_share_pending(world: World) -> None:
    prod = env_id(world.app, "prod")
    member = add_account(world.dsns.app, world.org.org_id, "member")
    before = grants_of(world, prod)
    args = {"app": "mcp-app", "env": "prod", "who": member}
    async with session(world.url, world.agent) as client:
        asked = await client.call_tool("request_share", args)
        again = await client.call_tool("request_share", args)

    assert not asked.is_error, asked.content
    out = asked.structured_content
    approval = out["approval"]
    assert out["requested"] is True
    assert out["created"] is True
    assert out["not_requested"] == ["widen_audience"]
    assert out["grants_version"] == before["grants_version"]
    assert approval["state"] == "pending"
    assert approval["kind"] == "agent_share"
    assert approval["environment_id"] == prod
    assert approval["requested_via_agent"] is True
    assert approval["requested_by_user_id"] == world.org.admin_user_id
    wanted = {"role": "user", "subject_kind": "user", "subject_id": member}
    assert approval["payload"] == {
        "grants_version": before["grants_version"],
        "grants": [*({k: g[k] for k in wanted} for g in before["grants"]), wanted],
    }
    assert again.structured_content["approval"]["id"] == approval["id"]
    assert again.structured_content["created"] is False
    assert grants_of(world, prod) == before

    audit = rows(
        world.dsns.app,
        world.org.org_id,
        "select action, actor_kind, actor_id, actor_via_agent, actor_client_id "
        "from ssc.audit_event where target_id = %s",
        (approval["id"],),
    )
    assert audit == [
        ("approval.requested", "user", world.org.admin_user_id, True, CLIENT_ID),
    ]


async def test_request_share_refused_or_not_needed(world: World) -> None:
    prod = env_id(world.app, "prod")
    member = add_account(world.dsns.app, world.org.org_id, "member")
    add_grant(world.dsns.app, world.org.org_id, prod, member, "user", world.org.admin_user_id)
    before = grants_of(world, prod)
    async with session(world.url, world.agent) as client:
        floor = await client.call_tool(
            "request_share", {"app": "mcp-app", "env": "preview", "who": member, "role": "user"}
        )
        same = await client.call_tool(
            "request_share", {"app": "mcp-app", "env": "prod", "who": member}
        )
        wider = await client.call_tool(
            "request_share", {"app": "mcp-app", "env": "prod", "who": member, "role": "builder"}
        )

    assert floor.is_error
    assert floor.structured_content["error"]["code"] == "VALIDATION_FAILED"
    assert floor.structured_content["error"]["status"] is None
    assert same.structured_content == {
        "requested": False,
        "environment_id": prod,
        "grants_version": before["grants_version"],
        "next": same.structured_content["next"],
    }
    grants = wider.structured_content["approval"]["payload"]["grants"]
    mine = [g for g in grants if g["subject_id"] == member]
    assert mine == [{"role": "builder", "subject_kind": "user", "subject_id": member}]
    assert grants_of(world, prod) == before


async def test_request_share_never_lowers_a_role(world: World) -> None:
    prod = env_id(world.app, "prod")
    builder = add_account(world.dsns.app, world.org.org_id, "member")
    add_grant(world.dsns.app, world.org.org_id, prod, builder, "builder", world.org.admin_user_id)
    before = grants_of(world, prod)
    async with session(world.url, world.agent) as client:
        floor = await client.call_tool(
            "request_share", {"app": "mcp-app", "env": "prod", "who": builder}
        )
        lower = await client.call_tool(
            "request_share", {"app": "mcp-app", "env": "prod", "who": builder, "role": "user"}
        )

    for result in (floor, lower):
        assert not result.is_error
        assert result.structured_content["requested"] is False
        assert "already has builder" in result.structured_content["next"]
    assert grants_of(world, prod) == before
    asked = rows(
        world.dsns.app,
        world.org.org_id,
        "select count(*) from ssc.approval_request where payload::text like %s",
        (f"%{builder}%",),
    )
    assert asked == [(0,)]


async def test_request_share_rereads_moved_grants(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = env_id(world.app, "prod")
    member = add_account(world.dsns.app, world.org.org_id, "member")
    version = grants_of(world, prod)["grants_version"]
    real, moved = tools.V1.post, []

    async def post(self: tools.V1, path: str, body: dict[str, Any], key: str) -> httpx2.Response:
        if path == "/v1/approvals" and not moved:
            moved.append(path)
            rows(
                world.dsns.app,
                world.org.org_id,
                "update ssc.environment set grants_version = grants_version + 1 where id = %s "
                "returning id",
                (prod,),
            )
        return await real(self, path, body, key)

    monkeypatch.setattr(tools.V1, "post", post)
    async with session(world.url, world.agent) as client:
        asked = await client.call_tool(
            "request_share", {"app": "mcp-app", "env": "prod", "who": member}
        )
    assert not asked.is_error, asked.content
    assert moved
    assert asked.structured_content["grants_version"] == version + 1
    assert asked.structured_content["approval"]["payload"]["grants_version"] == version + 1


async def test_request_connection(world: World) -> None:
    async with session(world.url, world.agent) as client:
        asked = await client.call_tool(
            "request_connection", {"app": "mcp-app", "connection": "ledger-db"}
        )
        bad = await client.call_tool(
            "request_connection", {"app": world.app["id"], "connection": "Ledger DB"}
        )

    assert not asked.is_error, asked.content
    approval = asked.structured_content["approval"]
    assert approval["kind"] == "connect_data_source"
    assert approval["environment_id"] == env_id(world.app, "prod")
    assert approval["subject_key"] == "ledger-db"
    assert approval["state"] == "pending"
    assert approval["requested_via_agent"] is True
    assert bad.is_error
    assert bad.structured_content["error"]["code"] == "VALIDATION_FAILED"
    assert bad.structured_content["error"]["status"] is not None


# ── releases and deploy ──────────────────────────────────────────────────────


async def test_list_releases(world: World) -> None:
    async with session(world.url, world.agent) as client:
        listed = await client.call_tool("list_releases", {"app": "mcp-app"})
        one = await client.call_tool("list_releases", {"app": "mcp-app", "limit": 1})
    assert not listed.is_error, listed.content
    ids = [r["release_id"] for r in listed.structured_content["items"]]
    assert world.release in ids
    assert len(one.structured_content["items"]) == 1


async def test_deploy_needs_size(world: World) -> None:
    async with session(world.url, world.agent) as client:
        r = await client.call_tool(
            "deploy", {"app": "mcp-app", "bundle_digest": "sha256:" + "0" * 64}
        )
    assert r.is_error
    assert r.structured_content["error"]["code"] == "VALIDATION_FAILED"
    assert r.structured_content["error"]["status"] is None


async def test_deploy_to_preview(world: World) -> None:
    org = world.org.org_id
    preview, prod = env_id(world.app, "preview"), env_id(world.app, "prod")
    data = tar_gz({"ssc.toml": b'schema = "ssc/v1"\n', "index.html": b"<p>hello</p>\n"})
    digest = "sha256:" + hashlib.sha256(data).hexdigest()

    preview_url = next(e["url"] for e in world.app["environments"] if e["name"] == "preview")
    assert preview_url is not None
    assert "--preview." in preview_url

    async with session(world.url, world.agent_token(scope="preview")) as client:
        pack = await client.call_tool("deploy", {"app": "mcp-app"})
        assert not pack.is_error, pack.content
        assert pack.structured_content["stage"] == "pack"
        assert pack.structured_content["bundle"]["max_bytes"] == 100 * 1024 * 1024
        key = pack.structured_content["idempotency_key"]
        args = {"app": "mcp-app", "bundle_digest": digest, "size_bytes": len(data)}
        args["idempotency_key"] = key

        upload = await client.call_tool("deploy", args)
        assert upload.structured_content["stage"] == "upload", upload.content
        target = upload.structured_content["upload"]
        put = httpx2.put(target["url"], content=data, headers=target["headers"])
        assert put.is_success, put.text

        building = await client.call_tool("deploy", args)
        assert building.structured_content["stage"] == "building", building.content
        build = building.structured_content["build_id"]
        assert build is not None
        rebuilding = await client.call_tool("deploy", args)
        assert rebuilding.structured_content["build_id"] == build
        other = await client.call_tool("deploy", {**args, "idempotency_key": new_key()})
        assert other.structured_content["stage"] == "building", other.content
        assert other.structured_content["build_id"] is None

        engine = make_engine(world.dsns.app)
        try:
            ports = Ports(engine=engine, build_driver=FakeBuildDriver())
            assert await run_build(ports, org_id=org, build_id=build) == "succeeded"
            # The same bundle built for prod, as promote leaves it: a newer release preview must
            # not run.
            prod_id = new_id("bld")
            rows(
                world.dsns.app,
                org,
                "insert into ssc.build (id, org_id, app_id, environment_id, bundle_id, actor_kind, "
                "actor_id) values (%s, %s, %s, %s, %s, 'user', %s) returning id",
                (
                    prod_id,
                    org,
                    world.app["id"],
                    prod,
                    building.structured_content["bundle_id"],
                    world.org.admin_user_id,
                ),
            )
            assert await run_build(ports, org_id=org, build_id=prod_id) == "succeeded"
        finally:
            await engine.dispose()

        status = await client.call_tool("get_status", {"app": "mcp-app", "build": build})
        built = status.structured_content["build"]
        assert built["state"] == "succeeded"

        deploying = await client.call_tool("deploy", args)
        replayed = await client.call_tool("deploy", args)
        op = deploying.structured_content["operation_id"]
        # What the deploy job does once the release is up.
        rows(
            world.dsns.app,
            org,
            "update ssc.deployment set state = 'healthy', finished_at = now() where id = %s "
            "returning id",
            (op,),
        )
        rows(
            world.dsns.app,
            org,
            "update ssc.environment set current_deployment_id = %s where id = %s returning id",
            (op, preview),
        )
        live = await client.call_tool("deploy", args)

    out = deploying.structured_content
    assert out["stage"] == "deploying", deploying.content
    assert out["release"]["release_id"] == built["release_id"]
    assert out["release"]["built_for_environment_id"] == preview
    assert out["location"] == f"/v1/operations/{op}"
    assert out["url"] == preview_url
    assert replayed.structured_content == out
    assert live.structured_content["stage"] == "live"
    assert live.structured_content["operation"]["operation_id"] == op
    assert live.structured_content["operation"]["state"] == "healthy"
    assert live.structured_content["url"] == preview_url

    bundle = rows(
        world.dsns.app,
        org,
        "select state, actor_via_agent, actor_client_id from ssc.bundle where digest = %s",
        (digest,),
    )
    assert bundle == [("stored", True, CLIENT_ID)]
    deployment = rows(
        world.dsns.app,
        org,
        "select kind, environment_id, release_id, actor_via_agent, actor_client_id "
        "from ssc.deployment where id = %s",
        (op,),
    )
    assert deployment == [("deploy", preview, built["release_id"], True, CLIENT_ID)]
    audit = rows(
        world.dsns.app,
        org,
        "select action, actor_via_agent, actor_client_id from ssc.audit_event "
        "where target_id in (%s, %s, %s) order by action",
        (build, op, building.structured_content["bundle_id"]),
    )
    assert audit == [
        ("build.started", True, CLIENT_ID),
        ("bundle.stored", True, CLIENT_ID),
        ("deploy.started", True, CLIENT_ID),
    ]


# ── contract ─────────────────────────────────────────────────────────────────


def test_openapi_is_unchanged_by_the_mcp_routes() -> None:
    assert not [p for p in build_spec()["paths"] if "mcp" in p or "well-known" in p]
    assert SPEC.read_text() == spec_json()


def test_create_app_builds_the_mcp_routes_quickly() -> None:
    started = time.monotonic()
    app = create_app(Settings.for_spec())
    elapsed = time.monotonic() - started
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/mcp", "/.well-known/oauth-protected-resource/mcp"} <= paths
    assert elapsed < 5


def test_building_leaves_root_logging_alone() -> None:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    root.handlers.clear()
    try:
        create_app(Settings.for_spec())
        assert root.handlers == []
        assert root.level == level
    finally:
        root.handlers[:] = handlers


def test_public_url_setting() -> None:
    env = {"SSC_DATABASE_DSN": "postgresql://x@db/ssc", "SSC_API_JWKS": '{"keys": []}'}
    env["SSC_API_ISSUER"] = ISSUER
    assert Settings.from_env(env).public_url == USER_AUDIENCE
    env["SSC_API_PUBLIC_URL"] = "https://api.example.test"
    assert Settings.from_env(env).public_url == "https://api.example.test"
