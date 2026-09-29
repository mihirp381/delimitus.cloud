"""SSC-042: promote builds for prod what preview runs, and is the only way to a prod release.

Uses test_deploy's bench: postgres:18, the fake build and runtime drivers.

  * same source as preview                -> test_promote_builds_preview_source_for_prod
  * prod only through promote              -> test_a_direct_prod_build_is_refused,
                                              test_a_prod_rollback_to_a_preview_build_is_refused
  * preview-only credentials               -> test_a_preview_scoped_token_cannot_promote
  * preconditions                          -> test_nothing_live_in_preview,
                                              test_a_stale_preview_release_id,
                                              test_a_stopped_app_cannot_promote,
                                              test_prod_in_flight_refuses_promote
  * replay                                 -> test_a_replay_returns_the_same_build
  * a failing gate starts no prod instance -> test_a_failing_prod_gate_starts_no_prod_instance
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import test_deploy
from httpx import Response
from ssc_testkit import SigningKey, assert_problem, auth, mint, new_key
from test_deploy import (
    NIGHTLY,
    Bench,
    SpyGate,
    audit_of,
    build_release,
    deploy,
    execute,
    get,
    manifest_of,
    operation,
    pointer,
    post,
    rows_of,
    run,
    seed_prod_build,
    set_prod_gate,
    start_build,
    start_deploy,
)

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER, REPLAYED_HEADER
from ssc_control.deploy.build_driver import fake_image_digest
from ssc_control.deploy.builds import run_build
from ssc_control.deploy.deployments import APPROVAL_REQUIRED
from ssc_control.runtime.driver import service_name

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

PUBLIC_ENV = {
    "preview": {"VITE_API": "https://preview.example"},
    "prod": {"VITE_API": "https://prod.example"},
}


def promote(b: Bench, body: dict[str, Any] | None = None, token: str | None = None) -> Response:
    return post(b, f"/v1/apps/{b.w.app}/promote", body or {}, token)


async def live_in_preview(b: Bench, **tables: Any) -> str:
    """A release built for preview and healthy there."""
    release = await build_release(b, b.w.preview, manifest_of(**tables))
    _, state = await deploy(b, b.w.preview, release)
    assert state == "healthy"
    return release


def release_row(b: Bench, release: str) -> dict[str, Any]:
    (row,) = rows_of(b.dsn, b.w.org, "select * from ssc.release where id = %s", release)
    return row


async def test_promote_builds_preview_source_for_prod(b: Bench) -> None:
    r1 = await live_in_preview(b, build={"public_env": PUBLIC_ENV}, **NIGHTLY)
    r = promote(b, {"preview_release_id": r1})
    assert r.status_code == 202, r.text
    build = r.json()["build_id"]
    assert r.headers["Location"] == f"/v1/builds/{build}"
    assert r.json()["state"] == "queued"
    started = audit_of(b, build)[0]
    assert started["action"] == "build.started"
    assert started["after"]["via"] == "promote"
    assert started["after"]["source_release_id"] == r1
    assert started["after"]["environment_id"] == b.w.prod

    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    out = get(b, f"/v1/builds/{build}").json()
    assert out["environment_id"] == b.w.prod
    prod_release = str(out["release_id"])
    source = release_row(b, r1)["source_digest"]
    released = get(b, f"/v1/apps/{b.w.app}/releases/{prod_release}").json()
    assert released["source_digest"] == source
    assert released["built_for_environment_id"] == b.w.prod
    assert released["image_digest"] == fake_image_digest(source, "prod", PUBLIC_ENV["prod"])
    assert released["image_digest"] != release_row(b, r1)["image_digest"]
    assert b.builds.requests[-1].public_env == PUBLIC_ENV["prod"]

    # The second step is the ordinary forward deploy, which syncs prod's timers.
    op, state = await deploy(b, b.w.prod, prod_release)
    assert state == "healthy"
    assert pointer(b, b.w.prod) == op
    assert operation(b, op)["release_id"] == prod_release
    assert (b.w.prod, ("nightly",), b.w.builder) in b.timers.calls


async def test_a_direct_prod_build_is_refused(b: Bench) -> None:
    await live_in_preview(b)
    (bundle,) = rows_of(b.dsn, b.w.org, "select id from ssc.bundle")
    r = start_build(b, b.w.prod, bundle["id"])
    assert_problem(r, ErrorCode.PROD_REQUIRES_PROMOTE)
    assert_problem(
        start_build(b, b.w.prod, bundle["id"], b.t.admin), ErrorCode.PROD_REQUIRES_PROMOTE
    )
    assert (
        rows_of(b.dsn, b.w.org, "select id from ssc.build where environment_id = %s", b.w.prod)
        == []
    )


async def test_a_prod_rollback_to_a_preview_build_is_refused(b: Bench) -> None:
    r1 = await live_in_preview(b)
    for kind in ("deploy", "rollback"):
        r = start_deploy(b, b.w.prod, r1, kind)
        assert_problem(r, ErrorCode.RELEASE_ENVIRONMENT_MISMATCH)


async def test_a_preview_scoped_token_cannot_promote(b: Bench, signing_key: SigningKey) -> None:
    r1 = await live_in_preview(b)
    scoped = mint(
        signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", scope="preview"
    )
    assert_problem(promote(b, {"preview_release_id": r1}, scoped), ErrorCode.FORBIDDEN)
    assert (
        rows_of(b.dsn, b.w.org, "select id from ssc.build where environment_id = %s", b.w.prod)
        == []
    )


async def test_only_a_builder_on_prod_may_promote(b: Bench) -> None:
    await live_in_preview(b)
    assert_problem(promote(b, token=b.t.member), ErrorCode.FORBIDDEN)
    r = post(b, f"/v1/apps/{new_id('app')}/promote", {}, None)
    assert_problem(r, ErrorCode.NOT_FOUND)
    r = b.client.post(f"/v1/apps/{b.w.app}/promote", json={}, headers=auth(b.t.builder))
    assert_problem(r, ErrorCode.IDEMPOTENCY_KEY_REQUIRED)


async def test_nothing_live_in_preview(b: Bench) -> None:
    assert_problem(promote(b), ErrorCode.NOTHING_TO_PROMOTE)
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    # Accepted but not yet healthy: still nothing live.
    assert_problem(promote(b), ErrorCode.NOTHING_TO_PROMOTE)
    assert await run(b, op) == "healthy"
    assert promote(b).status_code == 202


async def test_a_stale_preview_release_id(b: Bench) -> None:
    r1 = await live_in_preview(b)
    r2 = await live_in_preview(b)
    assert_problem(promote(b, {"preview_release_id": r1}), ErrorCode.PRECONDITION_STALE)
    assert promote(b, {"preview_release_id": r2}).status_code == 202


async def test_a_replay_returns_the_same_build(b: Bench) -> None:
    await live_in_preview(b)
    key = new_key()
    path = f"/v1/apps/{b.w.app}/promote"
    headers = auth(b.t.builder, **{IDEMPOTENCY_HEADER: key})
    first = b.client.post(path, json={}, headers=headers)
    again = b.client.post(path, json={}, headers=headers)
    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json() == first.json()
    assert again.headers[REPLAYED_HEADER] == "true"
    builds = rows_of(b.dsn, b.w.org, "select id from ssc.build where environment_id = %s", b.w.prod)
    assert builds == [{"id": first.json()["build_id"]}]


async def test_a_stopped_app_cannot_promote(b: Bench) -> None:
    r1 = await live_in_preview(b)
    for status in ("disabled", "quarantined"):
        execute(b.dsn, b.w.org, "update ssc.app set status = %s where id = %s", status, b.w.app)
        assert_problem(promote(b, {"preview_release_id": r1}), ErrorCode.APP_NOT_ACTIVE)


async def test_prod_in_flight_refuses_promote(b: Bench) -> None:
    await live_in_preview(b)
    first = promote(b)
    assert first.status_code == 202
    # A second promote while the first build is queued.
    assert_problem(promote(b), ErrorCode.BUILD_IN_FLIGHT)
    build = first.json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    prod_release = get(b, f"/v1/builds/{build}").json()["release_id"]
    op = start_deploy(b, b.w.prod, prod_release).json()["operation_id"]
    assert_problem(promote(b), ErrorCode.DEPLOYMENT_IN_FLIGHT)
    assert await run(b, op) == "healthy"
    assert promote(b).status_code == 202


async def test_a_seeded_prod_build_also_counts_as_in_flight(b: Bench) -> None:
    await live_in_preview(b)
    (bundle,) = rows_of(b.dsn, b.w.org, "select id from ssc.bundle")
    seed_prod_build(b, bundle["id"])
    assert_problem(promote(b), ErrorCode.BUILD_IN_FLIGHT)


async def test_a_failing_prod_gate_starts_no_prod_instance(b: Bench) -> None:
    await live_in_preview(b)
    build = promote(b).json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    prod_release = get(b, f"/v1/builds/{build}").json()["release_id"]
    spy = SpyGate("waiting")
    set_prod_gate(b, spy)
    op = start_deploy(b, b.w.prod, prod_release).json()["operation_id"]
    assert await run(b, op, replace(b.ports, prod_gate=spy)) == "failed"
    assert operation(b, op)["failure_code"] == APPROVAL_REQUIRED
    assert spy.calls == [b.w.prod, b.w.prod]
    assert all(service != service_name(b.w.prod) for _, service in b.runtime.calls)
    assert service_name(b.w.prod) not in b.runtime.services
    assert pointer(b, b.w.prod) is None
    assert audit_of(b, op)[-1]["action"] == "deploy.failed"
