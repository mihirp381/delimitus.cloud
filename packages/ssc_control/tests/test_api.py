"""SSC-011: the API skeleton, driven end to end through FastAPI's TestClient against postgres:18.

Ticket "done when" checks:
  * a replayed POST returns the first result       -> test_replay_returns_first_result_byte_for_byte
  * a stale If-Match is refused                    -> test_stale_if_match_is_refused
  * a breaking OpenAPI change fails CI             -> test_openapi.py (no database needed)
Plus: every refusal is one problem shape, the claim is inside the transaction (a refused POST
leaves no claim), a concurrent duplicate blocks and then replays, deployments are operations,
credentials are rate-limited, audit rows join the transaction and the chain verifies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from ssc_testkit import (
    ISSUER,
    Dsns,
    SigningKey,
    assert_problem,
    auth,
    make_org,
    mint,
    new_key,
)

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.api.auth import API_TOKEN_TYP
from ssc_control.api.idempotency import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    Claimed,
    InFlight,
    Settled,
    claim,
    request_hash,
    settle,
)
from ssc_control.api.problems import REQUEST_ID_HEADER
from ssc_control.api.settings import INTERNAL_AUDIENCE, USER_AUDIENCE
from ssc_control.api.uow import Reply
from ssc_control.db import (
    CreatedOrg,
    bind_org_sync,
    bound_org,
    make_engine,
)

# ── database helpers ─────────────────────────────────────────────────────────


def rows(dsn: str, org: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        return conn.execute(sql, params).fetchall()


def add_user(dsn: str, org: str) -> str:
    uid = new_id("usr")
    rows(
        dsn,
        org,
        "insert into ssc.user_account (id, org_id, display_name, email, role) "
        "values (%s, %s, 'Some One', 'someone@example.com', 'member') returning id",
        (uid, org),
    )
    return uid


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


# ── application fixture ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    dsns: Dsns
    client: TestClient
    key: SigningKey
    org: CreatedOrg
    other: CreatedOrg
    user_token: str
    other_token: str
    workload_token: str


@pytest.fixture(scope="module")
def world(dsns: Dsns, signing_key: SigningKey) -> Iterator[World]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
    )
    org = make_org(dsns.app, "Org A")
    other = make_org(dsns.app, "Org B")
    with TestClient(create_app(settings)) as client:
        yield World(
            dsns=dsns,
            client=client,
            key=signing_key,
            org=org,
            other=other,
            user_token=mint(signing_key, org=org.org_id, sub=org.admin_user_id, jti="cred_a"),
            other_token=mint(signing_key, org=other.org_id, sub=other.admin_user_id, jti="cred_b"),
            workload_token=mint(
                signing_key,
                org=org.org_id,
                sub=new_id("env"),
                kind="workload",
                audience=INTERNAL_AUDIENCE,
                jti="cred_w",
            ),
        )


def create_app_via_api(w: World, slug: str, token: str | None = None) -> dict[str, Any]:
    r = w.client.post(
        "/v1/apps",
        json={"slug": slug},
        headers=auth(token or w.user_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert r.status_code == 201, r.text
    return r.json()


def env_of(app: dict[str, Any], name: str) -> dict[str, Any]:
    return next(e for e in app["environments"] if e["name"] == name)


# ── problems: one shape everywhere ───────────────────────────────────────────


def test_healthz_is_open_and_carries_a_request_id(world: World) -> None:
    r = world.client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert len(r.headers[REQUEST_ID_HEADER]) == 24


def test_well_formed_gateway_request_id_is_kept(world: World) -> None:
    r = world.client.get("/healthz", headers={REQUEST_ID_HEADER: "gw-abc123-000"})
    assert r.headers[REQUEST_ID_HEADER] == "gw-abc123-000"
    junk = world.client.get("/healthz", headers={REQUEST_ID_HEADER: "x y<script>"})
    assert len(junk.headers[REQUEST_ID_HEADER]) == 24


def test_missing_bearer_is_a_problem(world: World) -> None:
    assert_problem(world.client.get("/v1/whoami"), ErrorCode.UNAUTHENTICATED)


def test_unknown_path_and_wrong_method_are_problems(world: World) -> None:
    assert_problem(world.client.get("/v1/nope"), ErrorCode.NOT_FOUND)
    assert_problem(world.client.delete("/healthz"), ErrorCode.METHOD_NOT_ALLOWED)


def test_validation_failure_is_a_problem_with_no_field_echo(world: World) -> None:
    r = world.client.post(
        "/v1/apps",
        json={"slug": "Bad Slug", "extra": "field"},
        headers=auth(world.user_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    body = assert_problem(r, ErrorCode.VALIDATION_FAILED)
    assert "Bad Slug" not in json.dumps(body)


@pytest.mark.parametrize(
    "variant",
    ["wrong_audience", "wrong_typ", "unknown_kid", "expired", "wrong_issuer", "bad_org", "hs256"],
)
def test_bad_credentials_are_unauthenticated(world: World, variant: str) -> None:
    k, org, sub = world.key, world.org.org_id, world.org.admin_user_id
    if variant == "wrong_audience":
        token = mint(k, org=org, sub=sub, audience=INTERNAL_AUDIENCE)
    elif variant == "wrong_typ":
        token = mint(k, org=org, sub=sub, typ="JWT")
    elif variant == "unknown_kid":
        token = mint(k, org=org, sub=sub, kid="other")
    elif variant == "expired":
        token = mint(k, org=org, sub=sub, expires_in=-60)
    elif variant == "wrong_issuer":
        token = mint(k, org=org, sub=sub, issuer="https://elsewhere.test")
    elif variant == "bad_org":
        token = mint(k, org="not-an-org", sub=sub)
    else:
        token = jwt.encode(
            {"sub": sub}, "secret", algorithm="HS256", headers={"typ": API_TOKEN_TYP}
        )
    assert_problem(world.client.get("/v1/whoami", headers=auth(token)), ErrorCode.UNAUTHENTICATED)


def test_whoami_describes_the_credential(world: World) -> None:
    r = world.client.get("/v1/whoami", headers=auth(world.user_token))
    assert r.status_code == 200
    assert r.json() == {
        "org_id": world.org.org_id,
        "subject": world.org.admin_user_id,
        "kind": "user",
        "credential_id": "cred_a",
        "is_agent": False,
        "client_id": None,
    }


def test_user_credential_is_forbidden_on_internal(world: World) -> None:
    token = mint(
        world.key, org=world.org.org_id, sub=world.org.admin_user_id, audience=INTERNAL_AUDIENCE
    )
    r = world.client.post(
        "/internal/v1/heartbeat",
        json={"cell_label": "cellabcd"},
        headers=auth(token, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert_problem(r, ErrorCode.FORBIDDEN)


def test_internal_heartbeat_for_a_workload(world: World) -> None:
    headers = auth(world.workload_token, **{IDEMPOTENCY_HEADER: new_key()})
    body = {"cell_label": world.org.cell_label}
    r = world.client.post("/internal/v1/heartbeat", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["acknowledged"] is True
    assert r.json()["org_id"] == world.org.org_id
    again = world.client.post("/internal/v1/heartbeat", json=body, headers=headers)
    assert again.headers[REPLAYED_HEADER] == "true"
    assert again.content == r.content


# ── idempotency ──────────────────────────────────────────────────────────────


def test_post_without_idempotency_key_is_refused(world: World) -> None:
    r = world.client.post("/v1/apps", json={"slug": "nokey"}, headers=auth(world.user_token))
    assert_problem(r, ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    assert (
        rows(world.dsns.app, world.org.org_id, "select 1 from ssc.app where slug = 'nokey'") == []
    )


def test_overlong_idempotency_key_is_refused(world: World) -> None:
    r = world.client.post(
        "/v1/apps",
        json={"slug": "longkey"},
        headers=auth(world.user_token, **{IDEMPOTENCY_HEADER: "k" * 201}),
    )
    assert_problem(r, ErrorCode.VALIDATION_FAILED)


def test_create_app_makes_two_environments_and_an_audit_row(world: World) -> None:
    app = create_app_via_api(world, "alpha")
    assert app["owner_user_id"] == world.org.admin_user_id
    assert app["status"] == "active"
    assert sorted(e["name"] for e in app["environments"]) == ["preview", "prod"]
    assert all(e["grants_version"] == 1 for e in app["environments"])
    audit = rows(
        world.dsns.app,
        world.org.org_id,
        "select action, actor_kind, actor_id, target_kind, after from ssc.audit_event "
        "where target_id = %s",
        (app["id"],),
    )
    assert audit == [
        (
            "app.created",
            "user",
            world.org.admin_user_id,
            "app",
            {"slug": "alpha", "owner_user_id": world.org.admin_user_id},
        )
    ]


def test_replay_returns_first_result_byte_for_byte(world: World) -> None:
    headers = auth(world.user_token, **{IDEMPOTENCY_HEADER: new_key()})
    first = world.client.post("/v1/apps", json={"slug": "replayed"}, headers=headers)
    assert first.status_code == 201, first.text
    assert REPLAYED_HEADER not in first.headers
    second = world.client.post("/v1/apps", json={"slug": "replayed"}, headers=headers)
    assert second.status_code == 201
    assert second.headers[REPLAYED_HEADER] == "true"
    assert second.content == first.content
    assert second.headers["content-type"] == first.headers["content-type"]
    assert second.headers[REQUEST_ID_HEADER] != first.headers[REQUEST_ID_HEADER]
    count = rows(
        world.dsns.app, world.org.org_id, "select count(*) from ssc.app where slug = 'replayed'"
    )
    assert count == [(1,)]


def test_same_key_different_body_is_refused(world: World) -> None:
    headers = auth(world.user_token, **{IDEMPOTENCY_HEADER: new_key()})
    assert (
        world.client.post("/v1/apps", json={"slug": "same-key"}, headers=headers).status_code == 201
    )
    r = world.client.post("/v1/apps", json={"slug": "same-key-2"}, headers=headers)
    assert_problem(r, ErrorCode.IDEMPOTENCY_KEY_REUSED)


def test_keys_are_scoped_to_the_credential(world: World) -> None:
    key = new_key()
    a = create_app_via_api(world, "scoped-a", token=world.user_token)
    del a
    headers_a = auth(world.user_token, **{IDEMPOTENCY_HEADER: key})
    headers_b = auth(world.other_token, **{IDEMPOTENCY_HEADER: key})
    ra = world.client.post("/v1/apps", json={"slug": "scoped"}, headers=headers_a)
    rb = world.client.post("/v1/apps", json={"slug": "scoped"}, headers=headers_b)
    assert ra.status_code == 201 and rb.status_code == 201
    assert REPLAYED_HEADER not in rb.headers
    assert ra.json()["id"] != rb.json()["id"]


def test_refused_post_leaves_no_claim(world: World) -> None:
    create_app_via_api(world, "taken")
    key = new_key()
    r = world.client.post(
        "/v1/apps",
        json={"slug": "taken"},
        headers=auth(world.user_token, **{IDEMPOTENCY_HEADER: key}),
    )
    assert_problem(r, ErrorCode.ALREADY_EXISTS)
    claims = rows(
        world.dsns.app,
        world.org.org_id,
        "select 1 from ssc.idempotency_claim where key = %s",
        (key,),
    )
    assert claims == []
    # and the key is free again for a corrected request
    r2 = world.client.post(
        "/v1/apps",
        json={"slug": "taken-2"},
        headers=auth(world.user_token, **{IDEMPOTENCY_HEADER: key}),
    )
    assert r2.status_code == 201, r2.text


def test_visible_claimed_row_is_in_flight(world: World) -> None:
    key = new_key()
    body = json.dumps({"slug": "stuck"}).encode()
    rows(
        world.dsns.app,
        world.org.org_id,
        "insert into ssc.idempotency_claim (org_id, credential_id, key, request_hash) "
        "values (%s, 'cred_a', %s, %s) returning key",
        (world.org.org_id, key, request_hash("POST", "/v1/apps", body)),
    )
    r = world.client.post(
        "/v1/apps", content=body, headers=auth(world.user_token, **{IDEMPOTENCY_HEADER: key})
    )
    assert_problem(r, ErrorCode.IDEMPOTENCY_IN_FLIGHT)


def test_concurrent_duplicates_yield_one_result(world: World) -> None:
    headers = auth(world.user_token, **{IDEMPOTENCY_HEADER: new_key()})
    results: list[Response] = []
    barrier = threading.Barrier(4)

    def go() -> None:
        barrier.wait()
        results.append(world.client.post("/v1/apps", json={"slug": "racing"}, headers=headers))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.status_code for r in results] == [201] * 4, [r.text for r in results]
    assert len({r.content for r in results}) == 1
    assert sum(REPLAYED_HEADER in r.headers for r in results) == 3
    count = rows(
        world.dsns.app, world.org.org_id, "select count(*) from ssc.app where slug = 'racing'"
    )
    assert count == [(1,)]


def test_second_claim_blocks_until_the_first_settles(world: World) -> None:
    """The primary-key wait is what makes 'one result' true; prove it at the database level."""
    org, key = world.org.org_id, new_key()
    h = request_hash("POST", "/v1/apps", b"{}")
    reply = Reply(status=201, body={"id": "x"}, headers={})

    async def go() -> tuple[object, object]:
        a, b = make_engine(world.dsns.app), make_engine(world.dsns.app)
        try:
            async with bound_org(a, org) as conn_a:
                first = await claim(conn_a, org_id=org, credential_id="c", key=key, hash_=h)
                assert isinstance(first, Claimed)

                async def second() -> object:
                    async with bound_org(b, org) as conn_b:
                        return await claim(conn_b, org_id=org, credential_id="c", key=key, hash_=h)

                task = asyncio.create_task(second())
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), 0.5)
                assert not task.done()
                await settle(conn_a, org_id=org, credential_id="c", key=key, reply=reply)
            return first, await asyncio.wait_for(task, 5)
        finally:
            await a.dispose()
            await b.dispose()

    first, second = asyncio.run(go())
    assert isinstance(first, Claimed)
    assert isinstance(second, Settled)
    assert second.reply == reply
    assert not isinstance(second, InFlight)


# ── apps: reads and isolation ────────────────────────────────────────────────


def test_list_and_get_apps(world: World) -> None:
    app = create_app_via_api(world, "listed")
    listed = world.client.get("/v1/apps", headers=auth(world.user_token))
    assert listed.status_code == 200
    assert {
        "id": app["id"],
        "slug": "listed",
        "owner_user_id": app["owner_user_id"],
        "status": "active",
    } in listed.json()["apps"]
    got = world.client.get(f"/v1/apps/{app['id']}", headers=auth(world.user_token))
    assert got.status_code == 200
    assert got.json() == app


def test_another_org_cannot_see_the_app(world: World) -> None:
    app = create_app_via_api(world, "private")
    r = world.client.get(f"/v1/apps/{app['id']}", headers=auth(world.other_token))
    assert_problem(r, ErrorCode.NOT_FOUND)
    listed = world.client.get("/v1/apps", headers=auth(world.other_token))
    assert app["id"] not in {a["id"] for a in listed.json()["apps"]}


def test_malformed_id_is_a_validation_problem(world: World) -> None:
    assert_problem(
        world.client.get("/v1/apps/not-an-id", headers=auth(world.user_token)),
        ErrorCode.VALIDATION_FAILED,
    )


def test_workload_cannot_create_an_app(world: World) -> None:
    token = mint(world.key, org=world.org.org_id, sub=new_id("env"), kind="workload")
    r = world.client.post(
        "/v1/apps",
        json={"slug": "by-robot"},
        headers=auth(token, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert_problem(r, ErrorCode.FORBIDDEN)


# ── sharing rules behind If-Match ────────────────────────────────────────────


def grants_url(app: dict[str, Any], name: str = "prod") -> str:
    return f"/v1/apps/{app['id']}/environments/{env_of(app, name)['id']}/grants"


def test_get_grants_returns_an_etag(world: World) -> None:
    app = create_app_via_api(world, "etag")
    r = world.client.get(grants_url(app), headers=auth(world.user_token))
    assert r.status_code == 200
    assert r.headers["ETag"] == '"1"'
    assert r.json() == {
        "environment_id": env_of(app, "prod")["id"],
        "grants_version": 1,
        "grants": [],
    }


def test_put_grants_without_if_match_is_refused(world: World) -> None:
    app = create_app_via_api(world, "nomatch")
    r = world.client.put(grants_url(app), json={"grants": []}, headers=auth(world.user_token))
    assert_problem(r, ErrorCode.PRECONDITION_REQUIRED)


def test_stale_if_match_is_refused(world: World) -> None:
    app = create_app_via_api(world, "stale")
    member = add_user(world.dsns.app, world.org.org_id)
    url = grants_url(app)
    body = {"grants": [{"role": "user", "subject_kind": "user", "subject_id": member}]}
    ok = world.client.put(url, json=body, headers=auth(world.user_token, **{"If-Match": '"1"'}))
    assert ok.status_code == 200, ok.text
    assert ok.headers["ETag"] == '"2"'
    assert ok.json()["grants_version"] == 2
    stale = world.client.put(
        url, json={"grants": []}, headers=auth(world.user_token, **{"If-Match": '"1"'})
    )
    assert_problem(stale, ErrorCode.PRECONDITION_STALE)
    # nothing changed
    assert world.client.get(url, headers=auth(world.user_token)).headers["ETag"] == '"2"'
    assert len(world.client.get(url, headers=auth(world.user_token)).json()["grants"]) == 1


def test_grant_edits_are_diffed_and_audited(world: World) -> None:
    app = create_app_via_api(world, "audited-grants")
    member = add_user(world.dsns.app, world.org.org_id)
    url = grants_url(app)
    first = world.client.put(
        url,
        json={
            "grants": [
                {"role": "user", "subject_kind": "user", "subject_id": member},
                {"role": "user", "subject_kind": "org", "subject_id": None},
            ]
        },
        headers=auth(world.user_token, **{"If-Match": '"1"'}),
    )
    assert first.status_code == 200, first.text
    kept = next(g for g in first.json()["grants"] if g["subject_kind"] == "org")
    second = world.client.put(
        url,
        json={"grants": [{"role": "user", "subject_kind": "org"}]},
        headers=auth(world.user_token, **{"If-Match": first.headers["ETag"]}),
    )
    assert second.status_code == 200, second.text
    assert second.json()["grants"] == [kept]  # the unchanged grant keeps its id
    actions = rows(
        world.dsns.app,
        world.org.org_id,
        "select action from ssc.audit_event where target_kind = 'app_grant' "
        "and (after->>'environment_id' = %s or target_id in "
        "(select target_id from ssc.audit_event where after->>'environment_id' = %s)) "
        "order by seq",
        (env_of(app, "prod")["id"], env_of(app, "prod")["id"]),
    )
    assert [a[0] for a in actions] == ["grant.added", "grant.added", "grant.removed"]


def test_grant_for_unknown_user_is_a_reference_problem(world: World) -> None:
    app = create_app_via_api(world, "bad-ref")
    r = world.client.put(
        grants_url(app),
        json={"grants": [{"role": "user", "subject_kind": "user", "subject_id": new_id("usr")}]},
        headers=auth(world.user_token, **{"If-Match": '"1"'}),
    )
    assert_problem(r, ErrorCode.REFERENCE_NOT_FOUND)
    assert (
        world.client.get(grants_url(app), headers=auth(world.user_token)).headers["ETag"] == '"1"'
    )


def test_inconsistent_grant_subject_is_a_validation_problem(world: World) -> None:
    app = create_app_via_api(world, "bad-grant")
    r = world.client.put(
        grants_url(app),
        json={"grants": [{"role": "user", "subject_kind": "org", "subject_id": new_id("usr")}]},
        headers=auth(world.user_token, **{"If-Match": '"1"'}),
    )
    assert_problem(r, ErrorCode.VALIDATION_FAILED)


# ── deployments are long-running operations ──────────────────────────────────


def test_deployment_is_accepted_and_pollable(world: World) -> None:
    app = create_app_via_api(world, "deployable")
    release = add_release(world.dsns.app, world.org.org_id, app["id"])
    url = f"/v1/apps/{app['id']}/environments/{env_of(app, 'prod')['id']}/deployments"
    headers = auth(world.user_token, **{IDEMPOTENCY_HEADER: new_key()})
    r = world.client.post(url, json={"release_id": release}, headers=headers)
    assert r.status_code == 202, r.text
    op = r.json()["operation_id"]
    assert r.headers["Location"] == f"/v1/operations/{op}"
    assert r.json()["state"] == "pending"

    replay = world.client.post(url, json={"release_id": release}, headers=headers)
    assert replay.status_code == 202
    assert replay.headers["Location"] == r.headers["Location"]
    assert replay.headers[REPLAYED_HEADER] == "true"

    status = world.client.get(r.headers["Location"], headers=auth(world.user_token))
    assert status.status_code == 200
    assert status.json()["state"] == "pending"
    assert status.json()["release_id"] == release
    assert status.json()["finished_at"] is None

    second = world.client.post(
        url,
        json={"release_id": release},
        headers=auth(world.user_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert_problem(second, ErrorCode.DEPLOYMENT_IN_FLIGHT)
    audit = rows(
        world.dsns.app,
        world.org.org_id,
        "select action from ssc.audit_event where target_id = %s",
        (op,),
    )
    assert audit == [("deploy.started",)]


def test_operation_of_another_org_is_not_found(world: World) -> None:
    app = create_app_via_api(world, "hidden-op")
    release = add_release(world.dsns.app, world.org.org_id, app["id"])
    url = f"/v1/apps/{app['id']}/environments/{env_of(app, 'preview')['id']}/deployments"
    r = world.client.post(
        url,
        json={"release_id": release},
        headers=auth(world.user_token, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert r.status_code == 202
    other = world.client.get(r.headers["Location"], headers=auth(world.other_token))
    assert_problem(other, ErrorCode.NOT_FOUND)


# ── audit chain ──────────────────────────────────────────────────────────────


def test_audit_chain_verifies_and_matches_the_head(world: World) -> None:
    events = rows(
        world.dsns.app,
        world.org.org_id,
        "select seq, canonical, prev_hash, hash from ssc.audit_event order by seq",
    )
    assert len(events) >= 5
    prev = bytes(32)
    for seq, canonical, prev_hash, digest in events:
        assert bytes(prev_hash) == prev
        assert bytes(digest) == hashlib.sha256(prev + bytes(canonical)).digest()
        assert json.loads(bytes(canonical))["seq"] == seq
        prev = bytes(digest)
    head = rows(world.dsns.app, world.org.org_id, "select seq, hash from ssc.audit_head")
    assert head == [(events[-1][0], events[-1][3])]


# ── rate limit ───────────────────────────────────────────────────────────────


def test_rate_limit_per_credential(dsns: Dsns, signing_key: SigningKey, world: World) -> None:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=2,
        rate_refill_per_second=0.001,
    )
    org = world.org
    a = mint(signing_key, org=org.org_id, sub=org.admin_user_id, jti="rate_a")
    b = mint(signing_key, org=org.org_id, sub=org.admin_user_id, jti="rate_b")
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/whoami", headers=auth(a)).status_code == 200
        assert client.get("/v1/whoami", headers=auth(a)).status_code == 200
        limited = client.get("/v1/whoami", headers=auth(a))
        assert_problem(limited, ErrorCode.RATE_LIMITED)
        assert int(limited.headers["Retry-After"]) >= 1
        # another credential of the same user is unaffected
        assert client.get("/v1/whoami", headers=auth(b)).status_code == 200
        # an unauthenticated caller never reaches the bucket
        assert_problem(client.get("/v1/whoami"), ErrorCode.UNAUTHENTICATED)


# ── settings ─────────────────────────────────────────────────────────────────


def test_settings_from_env() -> None:
    s = Settings.from_env(
        {
            "SSC_DATABASE_DSN": "postgresql://ssc_app:x@db/ssc",
            "SSC_API_JWKS": json.dumps({"keys": []}),
            "SSC_API_ISSUER": ISSUER,
            "SSC_API_RATE_CAPACITY": "10",
        }
    )
    assert s.database_dsn == "postgresql://ssc_app:x@db/ssc"
    assert s.jwks == {"keys": []}
    assert s.rate_capacity == 10
    assert s.user_audience == USER_AUDIENCE
    assert s.internal_audience == INTERNAL_AUDIENCE


def test_spec_settings_need_no_database() -> None:
    started = time.monotonic()
    app = create_app(Settings.for_spec())
    assert "/v1/apps" in app.openapi()["paths"]
    assert time.monotonic() - started < 5
