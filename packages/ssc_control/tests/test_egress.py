"""SSC-053: the egress allowlist, proxy credentials and the deploy step, against postgres:18, the
fake runtime and the cell agent's own routes.

Ticket "done when" checks that run without a cloud:
  * the allowlist is admin-edited, audited and compiled into the snapshot
                                            -> test_an_admin_lists_a_host_and_it_turns_on_egress,
                                               test_the_snapshot_carries_the_allowlist_and_...
  * the first allowed host brings the proxy with no human step
                                            -> test_an_admin_lists_a_host_and_it_turns_on_egress,
                                               test_an_approved_internet_host_is_listed
  * the console then shows the fixed IP     -> test_the_console_shows_the_fixed_outbound_address
  * patterns, no raw IPs                    -> test_a_host_that_is_not_an_entry_is_refused
  * the IT catalogue with high-risk flags   -> test_a_high_risk_host_needs_acknowledging,
                                               test_the_catalogue_says_what_is_listed
  * HTTPS_PROXY per app environment, two valid at once, NODE_USE_ENV_PROXY=1
                                            -> test_a_deploy_with_outbound_hosts_gets_its_...,
                                               test_the_snapshot_carries_the_allowlist_and_...
  * removing a host reaches the proxy       -> test_removing_a_host_is_audited_and_then_not_found
The proxy itself (listed, unlisted, raw IP, tunnel cut within the drain time) is tested against
Envoy in ``packages/ssc_egress/tests``; the machine, NAT and auto-heal in ``infra/tests``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast

import httpx2
import pytest
import test_deploy
from fastapi import FastAPI
from ssc_testkit import MemoryVault, SigningKey, assert_problem, auth, mint, new_key
from test_app_hosts import logged_evidence
from test_cell_resources import approve, done, jobs
from test_deploy import (
    Bench,
    build_release,
    get,
    manifest_of,
    operation,
    rows_of,
    run,
    start_deploy,
)

from ssc_agent.app import create_app as create_agent
from ssc_agent.egress import ProxyCredentials
from ssc_contracts.app_env import HTTPS_PROXY, NODE_USE_ENV_PROXY
from ssc_contracts.audit import ActorKind
from ssc_contracts.cells import CellResource
from ssc_contracts.egress import CATALOGUE, PLAIN_ENV, token_digest
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.audit import Actor
from ssc_control.db import bound_org
from ssc_control.domain.approval_rules import RequirementKind
from ssc_control.runtime.cell_egress import (
    AgentCellEgress,
    CellEgressError,
    FakeCellEgress,
    IssuedCredential,
    record_credential,
)
from ssc_control.snapshot.compiler import compile_document
from ssc_control.snapshot.service import compile_lock
from ssc_control.worker import CompositionError, cell_egress_from_env
from ssc_shared.runtime import secret_id, service_name

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

HOSTS = {"hosts": ["api.stripe.com"]}
PROXY = "10.20.4.10"
OUTBOUND = "192.0.2.10"


def put_host(b: Bench, host: str, token: str, **body: Any) -> httpx2.Response:
    return b.client.put(
        f"/v1/egress/hosts/{host}",
        json=body,
        headers=auth(token, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def delete_host(b: Bench, host: str, token: str) -> httpx2.Response:
    return b.client.delete(
        f"/v1/egress/hosts/{host}", headers=auth(token, **{IDEMPOTENCY_HEADER: new_key()})
    )


def egress_audit(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_id, target_id, before, after from ssc.audit_event "
        "where target_kind = 'egress_host' order by seq",
    )


def listed(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select host, added_by_user_id, approval_request_id from ssc.egress_host order by host",
    )


def egress_resource(b: Bench) -> dict[str, Any] | None:
    rows = rows_of(b.dsn, b.w.org, "select * from ssc.cell_resource where resource = 'egress'")
    return rows[0] if rows else None


async def tokens_for(url: str) -> str:
    return f"id-token-for-{url}"


@dataclass
class Agent:
    client: httpx2.AsyncClient
    vault: MemoryVault


def agent(*, proxy_address: str | None = PROXY) -> Agent:
    vault = MemoryVault()
    credentials = ProxyCredentials(vault, vault, proxy_address=proxy_address, outbound_ip=OUTBOUND)
    app = create_agent(cast("Any", None), egress=credentials)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://agent")
    return Agent(client, vault)


async def test_the_agent_writes_the_proxy_url_and_answers_without_the_token() -> None:
    env = new_id("env")
    cell = agent()
    r = await cell.client.post("/v1/egress/issue", json={"environment_id": env})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"credential_id", "sha1", "version"}
    url = cell.vault.latest(secret_id(service_name(env), HTTPS_PROXY))
    user = f"{env}.{body['credential_id']}"
    assert url.startswith(f"http://{user}:")
    assert url.endswith(f"@{PROXY}:3128")
    token = url.removeprefix(f"http://{user}:").removesuffix(f"@{PROXY}:3128")
    assert token not in r.text
    assert body["sha1"] == token_digest(token)
    assert body["version"] == "1"
    again = await cell.client.post("/v1/egress/issue", json={"environment_id": env})
    assert again.json()["version"] == "2"
    assert again.json()["credential_id"] != body["credential_id"]

    info = await cell.client.post("/v1/egress/info", json={})
    assert info.json() == {"proxy_address": PROXY, "outbound_ip": OUTBOUND}
    bad = await cell.client.post("/v1/egress/issue", json={"environment_id": "not-an-env"})
    assert (bad.status_code, bad.json()["code"]) == (400, "INVALID_REQUEST")
    assert (await cell.client.post("/v1/egress/rotate", json={})).status_code == 404
    await cell.client.aclose()

    unset = agent(proxy_address=None)
    r = await unset.client.post("/v1/egress/issue", json={"environment_id": env})
    assert (r.status_code, r.json()["code"]) == (503, "EGRESS_NOT_CONFIGURED")
    assert unset.vault.secrets == {}
    await unset.client.aclose()


async def test_the_agent_client_checks_what_comes_back() -> None:
    answers: list[httpx2.Response] = []
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return answers.pop(0)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    cell = AgentCellEgress("https://agent.example/", tokens_for, client=client)
    good = {"credential_id": "abcdefghij01", "sha1": "B" * 27 + "=", "version": "4"}
    answers.append(httpx2.Response(200, json=good))
    assert await cell.issue("env_x") == IssuedCredential(**good)
    assert str(seen[-1].url) == "https://agent.example/v1/egress/issue"
    assert seen[-1].headers["authorization"] == "Bearer id-token-for-https://agent.example"
    for wrong in ({**good, "sha1": "nope"}, {**good, "credential_id": "UPPER"}, {}):
        answers.append(httpx2.Response(200, json=wrong))
        with pytest.raises(CellEgressError):
            await cell.issue("env_x")
    answers.append(httpx2.Response(503, json={"code": "EGRESS_NOT_CONFIGURED", "message": "no"}))
    with pytest.raises(CellEgressError, match="503 EGRESS_NOT_CONFIGURED"):
        await cell.issue("env_x")
    answers.append(httpx2.Response(200, json={"proxy_address": PROXY, "outbound_ip": ""}))
    info = await cell.info()
    assert (info.proxy_address, info.outbound_ip) == (PROXY, None)
    await cell.aclose()


def test_the_worker_composes_cell_egress() -> None:
    assert cell_egress_from_env({}) is None
    assert isinstance(cell_egress_from_env({"SSC_RUNTIME_DRIVER": "fake"}), FakeCellEgress)
    with pytest.raises(CompositionError):
        cell_egress_from_env({"SSC_RUNTIME_DRIVER": "cell_agent", "SSC_CELL_AGENT_URL": "http://a"})
    made = cell_egress_from_env(
        {"SSC_RUNTIME_DRIVER": "cell_agent", "SSC_CELL_AGENT_URL": "https://agent.example"}
    )
    assert isinstance(made, AgentCellEgress)


async def test_an_admin_lists_a_host_and_it_turns_on_egress(b: Bench) -> None:
    assert egress_resource(b) is None
    r = put_host(b, "api.stripe.com", b.t.admin)
    assert r.status_code == 200, r.text
    (host,) = r.json()["hosts"]
    assert (host["host"], host["high_risk"], host["added_by_user_id"]) == (
        "api.stripe.com",
        False,
        b.w.admin,
    )
    resource = egress_resource(b)
    assert resource is not None
    assert (resource["state"], resource["cause"]) == ("requested", "admin")
    assert len(jobs(b, f"cellres:{b.w.org}:{CellResource.EGRESS.value}")) == 1
    (added,) = egress_audit(b)
    assert (added["action"], added["actor_id"], added["target_id"]) == (
        "org.updated",
        b.w.admin,
        "api.stripe.com",
    )
    assert added["after"] == {
        "host": "api.stripe.com",
        "high_risk": False,
        "approval_request_id": None,
    }
    assert added["before"] is None
    assert len(jobs(b, compile_lock(b.w.org))) == 1

    assert put_host(b, "api.stripe.com", b.t.admin).status_code == 200
    assert len(egress_audit(b)) == 1
    assert put_host(b, "*.atlassian.net", b.t.admin).status_code == 200
    assert [h["host"] for h in get(b, "/v1/egress", b.t.member).json()["hosts"]] == [
        "*.atlassian.net",
        "api.stripe.com",
    ]
    assert len(jobs(b, f"cellres:{b.w.org}:{CellResource.EGRESS.value}")) == 1


async def test_only_an_admin_outside_an_agent_session_changes_the_list(
    b: Bench, signing_key: SigningKey
) -> None:
    assert_problem(put_host(b, "api.stripe.com", b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(delete_host(b, "api.stripe.com", b.t.member), ErrorCode.FORBIDDEN)
    session = mint(
        signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True
    )
    assert_problem(put_host(b, "api.stripe.com", session), ErrorCode.AGENT_SESSION_REFUSED)
    assert_problem(delete_host(b, "api.stripe.com", session), ErrorCode.AGENT_SESSION_REFUSED)
    assert listed(b) == []
    assert egress_resource(b) is None
    assert get(b, "/v1/egress", b.t.member).status_code == 200
    assert get(b, "/v1/egress/catalogue", b.t.member).status_code == 200


async def test_a_host_that_is_not_an_entry_is_refused(
    b: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    for host in ("10.0.0.1", "api.stripe.com:443", "*.*.example.com", "api.*.example.com"):
        r = put_host(b, host, b.t.admin)
        assert_problem(r, ErrorCode.VALIDATION_FAILED)
        assert logged_evidence(caplog, r)["host"] == host
    assert listed(b) == []
    assert egress_audit(b) == []


async def test_a_high_risk_host_needs_acknowledging(
    b: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    r = put_host(b, "api.openai.com", b.t.admin)
    assert_problem(r, ErrorCode.VALIDATION_FAILED)
    assert logged_evidence(caplog, r) == {
        "host": "api.openai.com",
        "high_risk": True,
        "required": "acknowledge_high_risk",
    }
    assert listed(b) == []
    r = put_host(b, "api.openai.com", b.t.admin, acknowledge_high_risk=True)
    assert r.status_code == 200, r.text
    assert r.json()["hosts"][0]["high_risk"] is True
    assert egress_audit(b)[0]["after"]["high_risk"] is True


async def test_removing_a_host_is_audited_and_then_not_found(b: Bench) -> None:
    assert put_host(b, "api.stripe.com", b.t.admin).status_code == 200
    done(b.dsn, compile_lock(b.w.org))
    r = delete_host(b, "api.stripe.com", b.t.admin)
    assert r.status_code == 200, r.text
    assert r.json()["hosts"] == []
    removed = egress_audit(b)[-1]
    assert (removed["action"], removed["target_id"], removed["after"]) == (
        "org.updated",
        "api.stripe.com",
        None,
    )
    assert removed["before"]["host"] == "api.stripe.com"
    assert len(jobs(b, compile_lock(b.w.org))) == 1
    assert_problem(delete_host(b, "api.stripe.com", b.t.admin), ErrorCode.NOT_FOUND)
    assert egress_resource(b) is not None


async def test_the_catalogue_says_what_is_listed(b: Bench) -> None:
    assert put_host(b, "api.github.com", b.t.admin).status_code == 200
    entries = get(b, "/v1/egress/catalogue", b.t.member).json()["entries"]
    assert [e["host"] for e in entries] == [e.host for e in CATALOGUE]
    by_host = {e["host"]: e for e in entries}
    assert by_host["api.github.com"]["listed"] is True
    assert by_host["api.stripe.com"]["listed"] is False
    assert {h for h, e in by_host.items() if e["high_risk"]} >= {
        "api.openai.com",
        "api.anthropic.com",
        "wetransfer.com",
    }
    assert all(e["note"] for e in entries if e["high_risk"])


async def test_the_console_shows_the_fixed_outbound_address(b: Bench) -> None:
    before = get(b, "/v1/egress", b.t.member).json()
    assert (before["outbound_ip"], before["proxy_address"]) == (None, None)
    app = cast("FastAPI", b.client.app)
    app.state.runtime = replace(app.state.runtime, cell_egress=FakeCellEgress())
    after = get(b, "/v1/egress", b.t.member).json()
    assert (after["outbound_ip"], after["proxy_address"]) == (OUTBOUND, PROXY)

    class Down(FakeCellEgress):
        async def info(self) -> Any:
            raise CellEgressError("the agent is down")

    app.state.runtime = replace(app.state.runtime, cell_egress=Down())
    down = get(b, "/v1/egress", b.t.member)
    assert down.status_code == 200
    assert down.json()["outbound_ip"] is None


async def test_an_approved_internet_host_is_listed(b: Bench) -> None:
    await approve(b, RequirementKind.ENABLE_INTERNET_HOSTS, "api.stripe.com")
    (row,) = listed(b)
    (request,) = rows_of(b.dsn, b.w.org, "select id from ssc.approval_request")
    assert (row["host"], row["added_by_user_id"], row["approval_request_id"]) == (
        "api.stripe.com",
        b.w.approver,
        request["id"],
    )
    resource = egress_resource(b)
    assert resource is not None
    assert resource["cause"] == "egress_approved"
    assert egress_audit(b)[0]["after"]["approval_request_id"] == request["id"]
    await approve(b, RequirementKind.ENABLE_INTERNET_HOSTS, "api.github.com", "denied")
    await approve(b, RequirementKind.ENABLE_INTERNET_HOSTS, "10.0.0.1")
    assert [r["host"] for r in listed(b)] == ["api.stripe.com"]


async def compiled(b: Bench) -> Any:
    async with bound_org(b.ports.engine, b.w.org) as conn:
        return await compile_document(conn, b.w.org, version=1, compiled_at=datetime.now(UTC))


async def test_the_snapshot_carries_the_allowlist_and_the_newest_two_credentials(
    b: Bench,
) -> None:
    doc = await compiled(b)
    assert doc.egress is None
    assert "egress" not in doc.model_dump(mode="json", exclude_none=True)
    for host in ("hooks.slack.com", "*.atlassian.net", "api.stripe.com"):
        assert put_host(b, host, b.t.admin).status_code == 200
    doc = await compiled(b)
    assert doc.egress is not None
    assert doc.egress.hosts == ("*.atlassian.net", "api.stripe.com", "hooks.slack.com")
    assert doc.egress.credentials == {}

    actor = Actor(ActorKind.USER, b.w.builder)
    ids = [f"cred{n:08d}" for n in range(3)]
    for n, credential_id in enumerate(ids):
        issued = IssuedCredential(
            credential_id=credential_id, sha1=chr(ord("A") + n) * 27 + "=", version=str(n + 1)
        )
        async with bound_org(b.ports.engine, b.w.org) as conn:
            await record_credential(
                conn, org_id=b.w.org, environment_id=b.w.preview, issued=issued, actor=actor
            )
        await asyncio.sleep(0.01)
    doc = await compiled(b)
    held = doc.egress.credentials[b.w.preview]
    assert [(c.credential_id, c.sha1) for c in held] == [
        (ids[1], "B" * 27 + "="),
        (ids[2], "C" * 27 + "="),
    ]
    assert b.w.prod not in doc.egress.credentials
    stored = rows_of(b.dsn, b.w.org, "select credential_id from ssc.egress_credential")
    assert sorted(r["credential_id"] for r in stored) == ids[1:]
    (ref,) = rows_of(
        b.dsn,
        b.w.org,
        "select secret_version from ssc.secret_ref where environment_id = %s and name = %s",
        b.w.preview,
        HTTPS_PROXY,
    )
    assert ref["secret_version"] == "3"
    actions = rows_of(
        b.dsn,
        b.w.org,
        "select action, after from ssc.audit_event where target_kind = 'secret_ref' order by seq",
    )
    assert [a["action"] for a in actions] == ["secret.bound", "secret.rotated", "secret.rotated"]
    assert all("sha1" not in a["after"] for a in actions)


async def test_a_deploy_with_outbound_hosts_gets_its_credential_and_waits_for_the_snapshot(
    b: Bench,
) -> None:
    assert put_host(b, "api.stripe.com", b.t.admin).status_code == 200
    fake = FakeCellEgress()
    cell = test_deploy.Cell(b, b.w.preview)
    ports = replace(b.ports, cell_egress=fake, snapshot=cell)
    release = await build_release(b, b.w.preview, manifest_of(egress=HOSTS))
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run(b, op, ports) == "healthy"
    assert fake.calls == [b.w.preview]
    assert cell.events[:2] == ["request", "confirmed"]
    service = b.runtime.services[service_name(b.w.preview)]
    (revision,) = service.revisions
    assert dict(revision.secrets)[HTTPS_PROXY] == "1"
    env = dict(revision.env)
    assert {k: env[k] for k in PLAIN_ENV} == PLAIN_ENV
    assert env[NODE_USE_ENV_PROXY] == "1"
    (credential,) = rows_of(
        b.dsn, b.w.org, "select environment_id, secret_version from ssc.egress_credential"
    )
    assert (credential["environment_id"], credential["secret_version"]) == (b.w.preview, "1")

    again = await build_release(b, b.w.preview, manifest_of(egress=HOSTS))
    op = start_deploy(b, b.w.preview, again).json()["operation_id"]
    assert await run(b, op, ports) == "healthy"
    assert fake.calls == [b.w.preview]

    quiet = await build_release(b, b.w.preview, manifest_of())
    op = start_deploy(b, b.w.preview, quiet).json()["operation_id"]
    assert await run(b, op, ports) == "healthy"
    newest = service.revisions[-1]
    assert HTTPS_PROXY not in dict(newest.secrets)
    assert not set(PLAIN_ENV) & set(dict(newest.env))
    assert fake.calls == [b.w.preview]


async def test_when_the_cell_cannot_make_a_credential_the_deploy_fails(b: Bench) -> None:
    class Broken(FakeCellEgress):
        async def issue(self, environment_id: str) -> IssuedCredential:
            raise CellEgressError("the agent has no proxy address")

    assert put_host(b, "api.stripe.com", b.t.admin).status_code == 200
    ports = replace(b.ports, cell_egress=Broken(), snapshot=test_deploy.Cell(b, b.w.preview))
    release = await build_release(b, b.w.preview, manifest_of(egress=HOSTS))
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run(b, op, ports) == "failed"
    assert operation(b, op)["failure_code"] == "EGRESS_UNAVAILABLE"
    assert b.runtime.calls == []
    assert rows_of(b.dsn, b.w.org, "select 1 from ssc.egress_credential") == []
