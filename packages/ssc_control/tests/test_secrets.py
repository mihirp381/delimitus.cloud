"""SSC-026: app secrets, as references and versions; no value through the API, the control
database, a job, an audit row, a log or an agent.

Uses test_deploy's bench (postgres:18) and its Cloud Run emulator, with an in-memory Secret
Manager, the cell agent and the secret intake in process, and Google ID tokens signed by a test
key.

Ticket "done when" checks:
  * ``ssc secret get`` does not exist            -> test_no_route_returns_a_value,
                                                    test_no_mcp_tool_touches_secrets
                                                    (and ssc_cli's test_there_is_no_secret_get)
  * no secret value in audit rows after a deploy -> test_a_secret_goes_live_and_rotates_without_...
  * the control plane cannot read a value        -> test_no_seam_can_read_a_value,
                                                    test_no_code_calls_secret_access
                                                    (the deny rule itself: a live check)
Plus: refusals (an agent credential, an unconfigured cell, a platform name, a member, a
deployment in flight), the same version again, a secret set before the first deployment, the
intake's grant checks, the agent's one method, and migration 0021.
"""

from __future__ import annotations

import ast
import base64
import importlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import httpx2
import jwt
import psycopg
import pytest
import test_deploy
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import Response
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.engine import make_url
from ssc_testkit import Dsns, SigningKey, assert_problem, auth, find_secret_in, mint, new_key
from test_deploy import (
    AGENT,
    CELL_RUNTIME,
    Bench,
    Clock,
    access_token,
    agent_token,
    build_release,
    execute,
    get,
    pointer,
    post,
    rows_of,
    start_deploy,
)

from ssc_agent import intake as intake_module
from ssc_agent import secret_manager
from ssc_agent.app import create_app as create_agent
from ssc_agent.cloud_run import CloudRunDriver
from ssc_agent.intake import GOOGLE_CERTS, GoogleGrants, config_from_env, create_intake
from ssc_agent.secret_manager import (
    ACCESSOR_ROLE,
    CellSecretCustody,
    CellSecretWriter,
    SecretCustody,
    SecretWriter,
)
from ssc_conformance.cloud_run_emulator import PROJECT, CloudRunEmulator
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.mcp.tools import TOOLS
from ssc_control.db import MIGRATE_ROLE, downgrade, upgrade
from ssc_control.deploy.deployments import HEALTH_POLL_SECONDS, HealthWait, run_deployment
from ssc_control.runtime import secret_grants
from ssc_control.runtime.cell_agent import CellAgentDriver
from ssc_control.runtime.driver import service_name
from ssc_control.runtime.reconciler import load_desired, reconcile_env
from ssc_control.runtime.secret_grants import CellSecretGrants, SecretGrants
from ssc_control.runtime.specs import BundleReleaseSpecs
from ssc_shared.runtime import ServiceSpec, secret_id, spec_from_wire, spec_to_wire
from ssc_shared.secret_grants import GRANT_SECONDS, MAX_VALUE_BYTES

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

INTAKE = "https://ssc--secrets.cell.test"
CONTROL_SA = f"ssc-control@{PROJECT}.iam.gserviceaccount.com"
VALUE = "fake-secret-value-for-tests-0001"
ROTATED = "fake-secret-value-for-tests-0002"
NAME = "STRIPE_KEY"
KID = "google-test-1"
LATEST = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
SECRETS_ROOT = Path(__file__).resolve().parents[3] / "packages"


# ── a Secret Manager with no read ────────────────────────────────────────────


class SecretManagerEmulator:
    """Secret Manager v1's create, setIamPolicy, addVersion and delete, for
    ``httpx2.MockTransport``. It has no route that returns a value, as no SSC identity may call
    one. A policy may only name a service account the Cloud Run emulator has made."""

    def __init__(self, accounts: dict[str, Any]) -> None:
        self.accounts = accounts
        self.secrets: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append((request.method, request.url.path))
        body = json.loads(request.content) if request.content else {}
        parent = f"/v1/projects/{PROJECT}/secrets"
        path = request.url.path
        if request.method == "DELETE" and path.startswith(parent + "/"):
            gone = self.secrets.pop(path.removeprefix(parent + "/"), None)
            return httpx2.Response(200, json={}) if gone else _sm_error(404, "NOT_FOUND", path)
        if request.method != "POST" or not path.startswith(parent):
            return _sm_error(404, "NOT_FOUND", path)
        if path == parent:
            name = request.url.params.get("secretId", "")
            if name in self.secrets:
                return _sm_error(409, "ALREADY_EXISTS", f"{name} exists")
            self.secrets[name] = {**body, "versions": [], "policy": None}
            return httpx2.Response(200, json={"name": f"projects/{PROJECT}/secrets/{name}"})
        name, _, verb = path.removeprefix(parent + "/").partition(":")
        secret = self.secrets.get(name)
        if secret is None:
            return _sm_error(404, "NOT_FOUND", f"secret {name}")
        if verb == "setIamPolicy":
            members = [m for b in body["policy"]["bindings"] for m in b["members"]]
            known = {f"serviceAccount:{a['email']}" for a in self.accounts.values()}
            if not set(members) <= known:
                return _sm_error(400, "INVALID_ARGUMENT", "Service account does not exist.")
            secret["policy"] = body["policy"]
            return httpx2.Response(200, json=body["policy"])
        if verb == "addVersion":
            secret["versions"].append(base64.b64decode(body["payload"]["data"]))
            number = len(secret["versions"])
            version = f"projects/{PROJECT}/secrets/{name}/versions/{number}"
            return httpx2.Response(200, json={"name": version, "state": "ENABLED"})
        return _sm_error(404, "NOT_FOUND", path)


def _sm_error(status: int, reason: str, message: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"code": status, "status": reason, "message": message}}
    )


# ── Google ID tokens signed by a test key ────────────────────────────────────


@dataclass(frozen=True)
class Google:
    key: rsa.RSAPrivateKey

    def certs(self, request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == GOOGLE_CERTS
        jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return httpx2.Response(200, json={"keys": [{**jwk, "kid": KID, "alg": "RS256"}]})

    def token(
        self,
        audience: str,
        *,
        email: str = CONTROL_SA,
        issued: float | None = None,
        verified: bool = True,
    ) -> str:
        iat = int(time.time() if issued is None else issued)
        claims = {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "sub": "1234567890",
            "email": email,
            "email_verified": verified,
            "iat": iat,
            "exp": iat + 3600,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID})

    async def mint(self, audience: str) -> str:
        return self.token(audience)


@pytest.fixture(scope="module")
def google() -> Google:
    return Google(rsa.generate_private_key(public_exponent=65537, key_size=2048))


# ── the cell ─────────────────────────────────────────────────────────────────


class Spy(httpx2.AsyncBaseTransport):
    """Every request and response body that passes, to check none carries a value."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self.inner = inner
        self.seen: list[bytes] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(await request.aread())
        response = await self.inner.handle_async_request(request)
        self.seen.append(await response.aread())
        return response


@dataclass
class Cell:
    run: CloudRunEmulator
    sm: SecretManagerEmulator
    clock: Clock
    intake: httpx2.AsyncClient
    agent_spy: Spy
    grants: CellSecretGrants
    runtime: CellAgentDriver

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "secretmanager.googleapis.com":
            return self.sm.handler(request)
        return self.run.handler(request)


def make_agent(cell_handler: Any, run: CloudRunEmulator, sleep: Any) -> FastAPI:
    def mock() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(cell_handler))

    cloud_run = CloudRunDriver(CELL_RUNTIME, access_token, client=mock(), sleep=sleep)
    custody = CellSecretCustody(
        CELL_RUNTIME, access_token, cloud_run.ensure_identity, client=mock(), sleep=sleep
    )
    return create_agent(cloud_run, None, custody)


@pytest.fixture
async def cell(b: Bench, google: Google) -> AsyncIterator[Cell]:
    run = CloudRunEmulator()
    sm = SecretManagerEmulator(run.accounts)
    clock = Clock(run)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "secretmanager.googleapis.com":
            return sm.handler(request)
        return run.handler(request)

    api_agent = Spy(httpx2.ASGITransport(app=make_agent(handler, run, clock.driver_sleep)))
    grants = CellSecretGrants(
        agent_url=AGENT,
        intake_origin=INTAKE,
        agent_tokens=agent_token,
        grant_tokens=google.mint,
        client=httpx2.AsyncClient(transport=api_agent),
    )
    worker_agent = httpx2.ASGITransport(app=make_agent(handler, run, clock.driver_sleep))
    runtime = CellAgentDriver(AGENT, agent_token, client=httpx2.AsyncClient(transport=worker_agent))
    writer = CellSecretWriter(
        PROJECT, access_token, client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    checks = GoogleGrants(CONTROL_SA, transport=httpx2.MockTransport(google.certs))
    intake = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_intake(writer, checks, INTAKE))
    )
    app = cast("FastAPI", b.client.app)
    app.state.runtime = replace(app.state.runtime, secret_grants=grants)
    yield Cell(run, sm, clock, intake, api_agent, grants, runtime)
    await intake.aclose()
    await grants.aclose()
    await runtime.aclose()
    await checks.aclose()
    await writer.aclose()


# ── helpers ──────────────────────────────────────────────────────────────────


def secrets_path(b: Bench, env: str, name: str = NAME) -> str:
    return f"/v1/apps/{b.w.app}/environments/{env}/secrets/{name}"


def grant(b: Bench, env: str, name: str = NAME, token: str | None = None) -> Response:
    return post(b, f"{secrets_path(b, env, name)}/grants", {}, token)


def record(
    b: Bench, env: str, version: str, name: str = NAME, token: str | None = None
) -> Response:
    return b.client.put(
        secrets_path(b, env, name), json={"version": version}, headers=auth(token or b.t.builder)
    )


async def upload(cell: Cell, granted: Response, value: str) -> Response:
    target = granted.json()["upload"]
    return await cell.intake.put(target["url"], headers=target["headers"], content=value.encode())


async def set_secret(
    b: Bench, cell: Cell, env: str, value: str, replies: list[Response]
) -> Response:
    """``ssc secret set`` as the command line runs it: grant, upload, record."""
    granted = grant(b, env)
    assert granted.status_code == 201, granted.text
    uploaded = await upload(cell, granted, value)
    assert uploaded.status_code == 201, uploaded.text
    recorded = record(b, env, uploaded.json()["version"])
    replies += [granted, recorded]
    return recorded


async def run_through_the_cell(b: Bench, cell: Cell, op: str) -> str:
    ports = replace(b.ports, runtime_driver=cell.runtime)
    health = HealthWait(within=5.0, every=HEALTH_POLL_SECONDS, sleep=cell.clock.health_sleep)
    return await run_deployment(ports, org_id=b.w.org, deployment_id=op, health=health)


def secret_env(cell: Cell, env: str) -> list[dict[str, Any]]:
    template = cell.run.services[service_name(env)].body["template"]
    return [v for v in template["containers"][0]["env"] if "valueSource" in v]


def pinned(b: Bench, op: str) -> Any:
    (row,) = rows_of(b.dsn, b.w.org, "select secret_refs from ssc.deployment where id = %s", op)
    return row["secret_refs"]


# ── the whole path ───────────────────────────────────────────────────────────


async def test_a_secret_goes_live_and_rotates_without_its_value_anywhere(
    b: Bench, cell: Cell, dsns: Dsns, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    replies: list[Response] = []
    release = await build_release(b, b.w.preview)
    first = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, first) == "healthy"
    assert pinned(b, first) == {}
    svc = cell.run.services[service_name(b.w.preview)]
    (r1,) = [t["revision"] for t in svc.traffic_statuses]

    recorded = await set_secret(b, cell, b.w.preview, VALUE, replies)
    assert recorded.status_code == 202, recorded.text
    out = recorded.json()
    assert out | {"operation_id": None} == {
        "name": NAME,
        "version": "1",
        "changed": True,
        "operation_id": None,
    }
    assert recorded.headers["Location"] == f"/v1/operations/{out['operation_id']}"
    second = out["operation_id"]
    assert await run_through_the_cell(b, cell, second) == "healthy"
    assert pointer(b, b.w.preview) == second
    sid = secret_id(service_name(b.w.preview), NAME)
    ref = {"name": NAME, "valueSource": {"secretKeyRef": {"secret": sid, "version": "1"}}}
    assert secret_env(cell, b.w.preview) == [ref]
    (r2,) = [t["revision"] for t in svc.traffic_statuses]
    assert r2 != r1
    stored = cell.sm.secrets[sid]
    member = f"serviceAccount:{CELL_RUNTIME.identity(service_name(b.w.preview))}"
    assert stored["policy"] == {"bindings": [{"role": ACCESSOR_ROLE, "members": [member]}]}
    assert stored["replication"] == {"userManaged": {"replicas": [{"location": "us-central1"}]}}

    rotated = await set_secret(b, cell, b.w.preview, ROTATED, replies)
    assert rotated.status_code == 202, rotated.text
    third = rotated.json()["operation_id"]
    assert await run_through_the_cell(b, cell, third) == "healthy"
    assert secret_env(cell, b.w.preview)[0]["valueSource"]["secretKeyRef"]["version"] == "2"
    (r3,) = [t["revision"] for t in svc.traffic_statuses]
    assert r3 not in {r1, r2}
    assert [pinned(b, op) for op in (first, second, third)] == [{}, {NAME: "1"}, {NAME: "2"}]
    assert stored["versions"] == [VALUE.encode(), ROTATED.encode()]

    outcome = await reconcile_env(
        b.ports.engine, cell.runtime, BundleReleaseSpecs(), org_id=b.w.org, env_id=b.w.preview
    )
    assert outcome.kind == "converged"

    listed = get(b, f"/v1/apps/{b.w.app}/environments/{b.w.preview}/secrets")
    assert listed.json()["items"][0] | {"updated_at": None} == {
        "name": NAME,
        "version": "2",
        "live_version": "2",
        "updated_at": None,
    }
    replies += [listed, get(b, f"/v1/operations/{third}"), get(b, f"/v1/apps/{b.w.app}")]
    (ref_id,) = [r["id"] for r in rows_of(b.dsn, b.w.org, "select id from ssc.secret_ref")]
    audit = rows_of(
        b.dsn,
        b.w.org,
        "select action, before, after from ssc.audit_event where target_id = %s order by seq",
        ref_id,
    )
    after = {"environment_id": b.w.preview, "name": NAME}
    assert audit == [
        {"action": "secret.bound", "before": None, "after": after | {"version": "1"}},
        {
            "action": "secret.rotated",
            "before": after | {"version": "1"},
            "after": after | {"version": "2"},
        },
    ]

    for value in (VALUE, ROTATED):
        assert find_secret_in(dsns.superuser, value) == []
        assert not [r for r in replies if value in r.text]
        assert value not in caplog.text
        assert not [s for s in cell.agent_spy.seen if value.encode() in s]
        assert value not in json.dumps([svc.body, svc.revisions])
    verbs = {path.rpartition(":")[2] if ":" in path else "create" for _, path in cell.sm.calls}
    assert verbs == {"create", "setIamPolicy", "addVersion"}


async def test_find_secret_in_finds_every_form_it_looks_for(b: Bench, dsns: Dsns) -> None:
    assert start_deploy(b, b.w.preview, await build_release(b, b.w.preview)).status_code == 202
    for form in (VALUE, base64.b64encode(VALUE.encode()).decode(), VALUE.encode().hex()):
        with psycopg.connect(dsns.superuser) as conn:
            conn.execute(
                "update ssc.deployment set secret_refs = jsonb_build_object('LEAK', %s::text) "
                "where org_id = %s",
                (f"prefix {form} suffix", b.w.org),
            )
        assert find_secret_in(dsns.superuser, VALUE) == ["ssc.deployment"]
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute("update ssc.deployment set secret_refs = null where org_id = %s", (b.w.org,))
    assert find_secret_in(dsns.superuser, VALUE) == []


async def test_the_same_version_again_changes_nothing(b: Bench, cell: Cell) -> None:
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "healthy"
    first = await set_secret(b, cell, b.w.preview, VALUE, [])
    assert await run_through_the_cell(b, cell, first.json()["operation_id"]) == "healthy"
    again = record(b, b.w.preview, "1")
    assert again.status_code == 200, again.text
    assert again.json() == {"name": NAME, "version": "1", "changed": False, "operation_id": None}
    actions = rows_of(
        b.dsn, b.w.org, "select action from ssc.audit_event where action like 'secret.%%'"
    )
    assert actions == [{"action": "secret.bound"}]


async def test_a_secret_set_before_any_deployment_is_taken_by_the_first(
    b: Bench, cell: Cell
) -> None:
    recorded = await set_secret(b, cell, b.w.preview, VALUE, [])
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["operation_id"] is None
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "healthy"
    assert pinned(b, op) == {NAME: "1"}
    assert [v["name"] for v in secret_env(cell, b.w.preview)] == [NAME]


async def test_a_new_version_waits_for_a_deployment_in_flight(b: Bench, cell: Cell) -> None:
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "healthy"
    again = start_deploy(b, b.w.preview, release)
    assert again.status_code == 202, again.text
    granted = grant(b, b.w.preview)
    version = (await upload(cell, granted, VALUE)).json()["version"]
    assert_problem(record(b, b.w.preview, version), ErrorCode.DEPLOYMENT_IN_FLIGHT)
    assert rows_of(b.dsn, b.w.org, "select id from ssc.secret_ref") == []
    assert await run_through_the_cell(b, cell, again.json()["operation_id"]) == "healthy"
    assert record(b, b.w.preview, version).status_code == 202


async def test_a_foreign_secret_reference_is_drift_and_put_back(b: Bench, cell: Cell) -> None:
    await set_secret(b, cell, b.w.preview, VALUE, [])
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "healthy"
    own = secret_env(cell, b.w.preview)
    full = f"projects/{PROJECT}/secrets/{own[0]['valueSource']['secretKeyRef']['secret']}"

    service = service_name(b.w.preview)
    desired = await load_desired(
        b.ports.engine, BundleReleaseSpecs(), org_id=b.w.org, env_id=b.w.preview
    )
    assert isinstance(desired, ServiceSpec)

    def point_at(secret: str, *, serve: bool = False) -> None:
        def change(body: dict[str, Any]) -> None:
            body["template"].pop("revision", None)
            for var in body["template"]["containers"][0]["env"]:
                if var["name"] == NAME:
                    var["valueSource"]["secretKeyRef"]["secret"] = secret
            if serve:
                body["traffic"] = [{"type": LATEST, "percent": 100}]

        cell.run.edit(service, change)

    async def fingerprints() -> dict[str, str]:
        seen = await cell.runtime.observe(service)
        assert seen is not None
        return {r.revision: r.spec_fingerprint for r in seen.revisions}

    point_at(full)
    assert set((await fingerprints()).values()) == {desired.spec_fingerprint}
    before = await fingerprints()
    point_at("projects/another-project/secrets/stolen", serve=True)
    drifted = {r: f for r, f in (await fingerprints()).items() if r not in before}
    assert len(drifted) == 1
    assert desired.spec_fingerprint not in drifted.values()
    outcome = await reconcile_env(
        b.ports.engine, cell.runtime, BundleReleaseSpecs(), org_id=b.w.org, env_id=b.w.preview
    )
    assert outcome.kind == "changed"
    cell.run.settle()
    seen = await cell.runtime.observe(service)
    assert seen is not None
    (serving,) = [r for r in seen.revisions if r.traffic_percent == 100]
    assert serving.spec_fingerprint == desired.spec_fingerprint


def test_the_wire_spec_carries_secret_references() -> None:
    spec = replace(
        ServiceSpec(
            service=service_name("env_" + "a" * 20),
            image_digest="sha256:" + "a" * 64,
            port=8080,
            health_path="/",
            resource_class="small",
            env={},
            labels={},
            billing="request",
            timeout_seconds=300,
            concurrency=80,
            min_instances=0,
            max_instances=1,
        ),
        secrets={NAME: "7"},
    )
    wire = spec_to_wire(spec)
    assert wire["secrets"] == {NAME: "7"}
    assert spec_from_wire(json.loads(json.dumps(wire))) == spec


# ── refusals ─────────────────────────────────────────────────────────────────


async def test_an_agent_credential_cannot_touch_a_secret(
    b: Bench, cell: Cell, signing_key: SigningKey
) -> None:
    agent = mint(signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True)
    assert_problem(grant(b, b.w.preview, token=agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert_problem(record(b, b.w.preview, "1", token=agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert cell.sm.secrets == {}


async def test_secrets_need_a_builder_and_a_secret_name(b: Bench, cell: Cell) -> None:
    assert_problem(grant(b, b.w.preview, token=b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(record(b, b.w.preview, "1", token=b.t.member), ErrorCode.FORBIDDEN)
    for name in ("PORT", "SSC_ENV", "K_SERVICE", "DATABASE_URL"):
        assert_problem(grant(b, b.w.preview, name), ErrorCode.VALIDATION_FAILED)
    assert grant(b, b.w.preview, "stripe_key").status_code == 422
    assert record(b, b.w.preview, "0").status_code == 422
    assert record(b, b.w.preview, "latest").status_code == 422
    execute(b.dsn, b.w.org, "update ssc.app set status = 'disabled' where id = %s", b.w.app)
    assert_problem(grant(b, b.w.preview), ErrorCode.APP_NOT_ACTIVE)
    assert cell.sm.secrets == {}


async def test_an_unconfigured_cell_grants_nothing(b: Bench) -> None:
    assert_problem(grant(b, b.w.preview), ErrorCode.SECRETS_UNAVAILABLE)


async def test_a_failing_agent_grants_nothing(b: Bench, google: Google) -> None:
    def down(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"code": "UNAVAILABLE", "message": "agent down"})

    grants = CellSecretGrants(
        agent_url=AGENT,
        intake_origin=INTAKE,
        agent_tokens=agent_token,
        grant_tokens=google.mint,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(down)),
    )
    app = cast("FastAPI", b.client.app)
    app.state.runtime = replace(app.state.runtime, secret_grants=grants)
    assert_problem(grant(b, b.w.preview), ErrorCode.SECRETS_UNAVAILABLE)
    await grants.aclose()


# ── the intake ───────────────────────────────────────────────────────────────


async def test_the_intake_takes_a_value_only_with_a_fresh_grant_for_it(
    b: Bench, cell: Cell, google: Google
) -> None:
    granted = grant(b, b.w.preview)
    target = granted.json()["upload"]
    url = target["url"]
    sid = secret_id(service_name(b.w.preview), NAME)
    assert url.startswith(f"{INTAKE}/v1/secrets/{sid}?grant=")
    assert target["method"] == "PUT"
    expires = granted.json()["upload"]["expires_at"]
    assert expires.endswith("Z") or "+00:00" in expires
    other = secret_id(service_name(b.w.prod), NAME)

    def put(where: str, token: str | None, body: bytes = VALUE.encode()) -> Any:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return cell.intake.put(where, headers=headers, content=body)

    refused = [
        (await put(url, None)).status_code,
        (await put(url, "not-a-token")).status_code,
        (await put(url, google.token(url + "x"))).status_code,
        (await put(url.replace(sid, other), google.token(url))).status_code,
        (
            await put(url, google.token(url, email=f"intruder@{PROJECT}.iam.gserviceaccount.com"))
        ).status_code,
        (await put(url, google.token(url, verified=False))).status_code,
        (await put(url, google.token(url, issued=time.time() - GRANT_SECONDS - 60))).status_code,
        (await put(url, google.token(url), b"")).status_code,
        (await put(url, google.token(url), b"x" * (MAX_VALUE_BYTES + 1))).status_code,
        (
            await put(f"{INTAKE}/v1/secrets/ssc-a-other?grant={'n' * 32}", google.token(url))
        ).status_code,
        (await put(url.split("?")[0] + "?grant=short", google.token(url))).status_code,
    ]
    assert refused == [401, 403, 403, 403, 403, 403, 403, 400, 413, 400, 400]
    assert cell.sm.secrets.get(sid, {}).get("versions") == []

    answered = await upload(cell, granted, VALUE)
    assert answered.status_code == 201
    assert answered.json() == {"secret": sid, "version": "1"}
    assert VALUE not in answered.text


async def test_intake_errors_never_echo_a_value(google: Google) -> None:
    def refuse(_: httpx2.Request) -> httpx2.Response:
        return _sm_error(400, "INVALID_ARGUMENT", f"bad payload password={VALUE}")

    writer = CellSecretWriter(
        PROJECT, access_token, client=httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    )
    checks = GoogleGrants(CONTROL_SA, transport=httpx2.MockTransport(google.certs))
    app = create_intake(writer, checks, INTAKE)
    sid = secret_id(service_name("env_" + "a" * 20), NAME)
    url = f"{INTAKE}/v1/secrets/{sid}?grant={'n' * 32}"
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
        r = await client.put(
            url, headers={"Authorization": f"Bearer {google.token(url)}"}, content=VALUE.encode()
        )
    assert r.status_code == 502
    assert r.json()["code"] == "SECRETS_ERROR"
    assert VALUE not in r.text
    await writer.aclose()
    await checks.aclose()


def test_the_intake_needs_its_whole_configuration() -> None:
    env = {"SSC_CELL_PROJECT": PROJECT, "SSC_INTAKE_ORIGIN": INTAKE, "SSC_CONTROL_SA": CONTROL_SA}
    assert config_from_env(env) == {
        "project": PROJECT,
        "origin": INTAKE,
        "control_account": CONTROL_SA,
    }
    for name in env:
        with pytest.raises(ValueError, match=name):
            config_from_env({k: v for k, v in env.items() if k != name})
    with pytest.raises(ValueError, match="https"):
        config_from_env(env | {"SSC_INTAKE_ORIGIN": "http://ssc--secrets.cell.test"})


async def test_the_agent_has_one_secret_method(cell: Cell) -> None:
    app = make_agent(cell.handler, cell.run, cell.clock.driver_sleep)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=AGENT
    ) as client:
        for method in ("access", "get", "read", "versions", "addVersion", "remove", "delete"):
            assert (await client.post(f"/v1/secrets/{method}", json={})).status_code == 404
        r = await client.post("/v1/secrets/ensure", json={"secret": "projects-x"})
        assert r.status_code == 400
        assert (await client.get("/v1/secrets/ensure")).status_code == 405
    bare = create_agent(CloudRunDriver(CELL_RUNTIME, access_token), None, None)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=bare), base_url=AGENT
    ) as client:
        r = await client.post("/v1/secrets/ensure", json={"secret": "x"})
        assert (r.status_code, r.json()["code"]) == (503, "SECRETS_NOT_CONFIGURED")


async def test_custody_removes_a_secret_once_and_only_an_apps() -> None:
    run = CloudRunEmulator()
    sm = SecretManagerEmulator(run.accounts)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "secretmanager.googleapis.com":
            return sm.handler(request)
        return run.handler(request)

    def mock() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    cloud_run = CloudRunDriver(CELL_RUNTIME, access_token, client=mock())
    custody = CellSecretCustody(
        CELL_RUNTIME, access_token, cloud_run.ensure_identity, client=mock()
    )
    secret = secret_id(service_name("env_" + "r" * 20), NAME)
    await custody.ensure(secret)
    assert secret in sm.secrets
    await custody.remove(secret)
    assert secret not in sm.secrets
    await custody.remove(secret)
    assert [c for c in sm.calls if c[0] == "DELETE"] == [
        ("DELETE", f"/v1/projects/{PROJECT}/secrets/{secret}")
    ] * 2
    with pytest.raises(ValueError, match="not an SSC app secret id"):
        await custody.remove("ssc-control-key")


# ── no read, anywhere ────────────────────────────────────────────────────────

FORBIDDEN_FIELDS = frozenset({"value", "secret_value", "plaintext", "payload", "data"})


def _schema_fields(spec: dict[str, Any], node: Any, seen: set[str]) -> Iterator[str]:
    if isinstance(node, dict):
        node = cast("dict[str, Any]", node)
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                yield from _schema_fields(spec, spec["components"]["schemas"][name], seen)
        for key, child in node.items():
            if key == "properties":
                yield from cast("dict[str, Any]", child)
            yield from _schema_fields(spec, child, seen)
    elif isinstance(node, list):
        for child in cast("list[Any]", node):
            yield from _schema_fields(spec, child, seen)


def test_no_route_returns_a_value(b: Bench) -> None:
    spec = cast("FastAPI", b.client.app).openapi()
    secret_paths = {p: ops for p, ops in spec["paths"].items() if "/secrets" in p}
    base = "/v1/apps/{app_id}/environments/{environment_id}/secrets"
    assert {p: sorted(ops) for p, ops in secret_paths.items()} == {
        base: ["get"],
        base + "/{name}": ["put"],
        base + "/{name}/grants": ["post"],
    }
    for ops in secret_paths.values():
        for op in ops.values():
            fields = set(_schema_fields(spec, op.get("responses", {}), set()))
            assert not fields & FORBIDDEN_FIELDS, fields
    request = spec["paths"][base + "/{name}"]["put"]["requestBody"]
    assert set(_schema_fields(spec, request, set())) == {"version"}


def test_no_mcp_tool_touches_secrets() -> None:
    """The one secret tool, ``set_secret`` (SSC-048), only hands the person the ``ssc secret set``
    command: it calls no secret route, so no value or grant reaches an agent."""
    from ssc_cli import mcp_local  # noqa: PLC0415
    from ssc_control.api.mcp import tools as server_tools  # noqa: PLC0415

    for tools in (TOOLS, mcp_local.TOOLS):
        assert [t for t in tools if "secret" in t] == ["set_secret"]
    for module in (server_tools, mcp_local):
        assert "/secrets" not in inspect.getsource(module)


SEAMS = (
    (SecretCustody, {"ensure", "remove"}),
    (CellSecretCustody, {"ensure", "remove", "aclose"}),
    (SecretWriter, {"add_version"}),
    (CellSecretWriter, {"add_version", "aclose"}),
    (SecretGrants, {"grant"}),
    (CellSecretGrants, {"grant", "aclose"}),
)


def declared_members(cls: type) -> set[str]:
    """The public names a class itself declares."""
    return {n for n in vars(cls) if not n.startswith("_")}


def test_no_seam_can_read_a_value() -> None:
    for cls, allowed in SEAMS:
        assert declared_members(cls) == allowed, cls
    for cls, _ in SEAMS:
        for name in declared_members(cls):
            hints = inspect.signature(getattr(cls, name)).return_annotation
            assert "bytes" not in str(hints), (cls, name)

    class Planted(SecretWriter):
        async def add_version(self, secret: str, value: bytes) -> str:
            return "1"

        async def access(self, secret: str) -> bytes:
            return b""

    assert declared_members(Planted) != {"add_version"}


def test_no_code_calls_secret_access() -> None:
    """No source names Secret Manager's read: ``versions/<v>:access``."""
    readers = []
    for path in SECRETS_ROOT.glob("*/src/**/*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if ":access" in text or "accessSecretVersion" in text:
                    readers.append(f"{path.name}: {text[:60]}")
    assert readers == []
    for module in (secret_manager, intake_module, secret_grants):
        assert ":access" not in inspect.getsource(module)


def test_0021_downgrades_and_upgrades(dsns: Dsns) -> None:
    rev = importlib.import_module("ssc_control.db.migrations.versions.0021_secrets")
    name = f"m{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)
    columns = (
        "select table_name || '.' || column_name from information_schema.columns "
        "where table_schema = 'ssc' and (table_name, column_name) in "
        "(('deployment', 'secret_refs'), ('secret_ref', 'updated_at')) order by 1"
    )

    def present() -> list[str]:
        with psycopg.connect(dsn) as conn:
            return [r[0] for r in conn.execute(columns).fetchall()]

    upgrade(dsn)
    assert present() == ["deployment.secret_refs", "secret_ref.updated_at"]
    downgrade(dsn, rev.down_revision)
    assert present() == []
    upgrade(dsn)
    assert present() == ["deployment.secret_refs", "secret_ref.updated_at"]


async def test_a_secret_version_is_a_number_in_the_database(b: Bench) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        execute(
            b.dsn,
            b.w.org,
            "insert into ssc.secret_ref (id, org_id, environment_id, name, secret_version) "
            "values (%s, %s, %s, 'STRIPE_KEY', 'latest')",
            new_id("sec"),
            b.w.org,
            b.w.preview,
        )
    assert start_deploy(b, b.w.preview, await build_release(b, b.w.preview)).status_code == 202
    with pytest.raises(psycopg.errors.CheckViolation):
        execute(
            b.dsn,
            b.w.org,
            "update ssc.deployment set secret_refs = '[]'::jsonb where org_id = %s",
            b.w.org,
        )
