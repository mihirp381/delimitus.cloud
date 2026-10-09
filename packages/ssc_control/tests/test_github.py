"""SSC-047: the GitHub App. A push to a connected branch builds that commit and deploys it to
preview, the result and the preview address go on the commit as a check run, and promote waits
for the required checks.

Uses test_deploy's bench (postgres:18, the fake build and runtime drivers) and an in-memory
GitHub (``fake_github``) behind the real ``GitHubApp`` client, so tokens, signatures, redirects
and permissions are exercised the way GitHub serves them.

Ticket "done when" checks:
  * a push puts a preview address on the commit -> test_a_push_puts_the_preview_address_on_the_
                                                    commit (the whole path, timed against the
                                                    five minutes)
  * a fork PR produces no build                  -> test_a_fork_pull_request_builds_nothing
  * promote refused while a required check is red -> test_promote_is_refused_while_a_required_
                                                    check_is_red, test_a_release_without_a_
                                                    commit_cannot_pass_the_gate,
                                                    test_an_uploaded_bundle_declaring_a_green_
                                                    commit_cannot_pass_the_gate,
                                                    test_the_gate_never_opens_without_github
  * a forged webhook is refused                  -> test_a_forged_webhook_is_refused,
                                                    test_without_a_secret_every_delivery_is_
                                                    refused
Plus: the SSC-015 secret scan on a pushed commit, an older push never deployed over a newer
one, connecting and disconnecting (audited, never naming the repository, a builder on prod
only), the operator's binding CLI, the installation token cache and JWT, and the source
unpacker's refusals, and the worker's composition logging whether GitHub is ready (GA-7.2).
The migration is tested in test_control_db.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import tarfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import jwt
import pytest
import test_deploy
from fake_github import _PEM, _PUBLIC, APP_ID, BASE, FakeGitHub, Json, files_of, tarball_of
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, mint, new_key
from test_app_hosts import logged_evidence
from test_bundles import SERVICE_ROLE_ROLE, supabase_jwt
from test_deploy import (
    FIXTURES,
    MASTER,
    Bench,
    Hold,
    SpyTimers,
    Tokens,
    World,
    audit_of,
    deploy,
    execute,
    get,
    post,
    rows_of,
    run,
    seed_bundle,
    start_build,
    take_job,
)

from ssc_bundle.limits import BundleMalformedError, BundleTooLargeError, Limits
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.db import NewOrg, create_org, make_engine
from ssc_control.deploy.build_driver import FakeBuildDriver
from ssc_control.deploy.builds import run_build
from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.github import gate
from ssc_control.github.__main__ import main, run_bind
from ssc_control.github.client import CHECK_NAME, MAX_PAGES, PAGE, RepoRef
from ssc_control.github.links import BindError, RequiredCheck
from ssc_control.github.push import run_push
from ssc_control.github.source import unpack
from ssc_control.github.tasks import RUN_PUSH, push_lock
from ssc_control.github.webhook import MAX_BODY_BYTES
from ssc_control.metrics import metrics_port
from ssc_control.runtime.cells import STATIC_LABEL, OrgCell, StaticCells
from ssc_control.runtime.fake import FakeRuntimeDriver
from ssc_control.runtime.specs import BundleReleaseSpecs
from ssc_control.worker import CompositionError, Ports, github_from_env
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock
from ssc_shared.hosts import app_origin

world = test_deploy.world
tokens = test_deploy.tokens

SECRET = ("webhook-" + uuid.uuid4().hex).encode()
INSTALLATION = 5151
"""The installation the unit tests mint tokens for; each bench binds its own."""
OPERATOR = "op_support"
CI = ".github/workflows/ci.yml"
TEST = {"name": "test", "workflow": CI}
MANIFEST = b'schema = "ssc/v1"\n'
FIVE_MINUTES = 300.0


@dataclass
class G:
    b: Bench
    hub: FakeGitHub
    ports: Ports
    repo: Json
    installation: int


@pytest.fixture
async def g(
    dsns: Dsns, signing_key: SigningKey, world: World, tokens: Tokens, tmp_path: Path
) -> AsyncIterator[G]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        metrics_key=MASTER,
        github_webhook_secret=SECRET,
    )
    hub = FakeGitHub()
    installation = uuid.uuid4().int % 2_000_000_000 + 1
    repo = hub.repo("Acme/ledger", installation)
    api_github, worker_github = hub.client(), hub.client()
    engine = make_engine(dsns.app)
    hold, timers = Hold(), SpyTimers()
    runtime, builds = FakeRuntimeDriver(sleep=hold), FakeBuildDriver()
    ports = Ports(
        engine=engine,
        cells=StaticCells(OrgCell(label=STATIC_LABEL, runtime=runtime, build=builds)),
        release_specs=BundleReleaseSpecs(),
        timers=timers,
        prod_gate=approvals_prod_gate(),
        metrics=metrics_port(MASTER),
    )
    signer = UrlSigner({"k1": MASTER}, active="k1", clock=SystemClock())
    store = FsBlobStore(tmp_path / "blobs", signer=signer, base_url="https://blobs.test/v1/blobs")
    pushing = replace(ports, blob_store=store, github=worker_github)
    with TestClient(create_app(settings, github=api_github)) as client:
        bench = Bench(client, world, tokens, dsns.app, runtime, hold, builds, timers, ports)
        yield G(bench, hub, pushing, repo, installation)
    await worker_github.aclose()
    await api_github.aclose()
    await engine.dispose()


def sha_of(n: int) -> str:
    return hashlib.sha1(f"commit-{n}".encode()).hexdigest()  # noqa: S324  (a fake commit id)


def app_files(**extra: bytes) -> dict[str, bytes]:
    return {"ssc.toml": MANIFEST, **files_of(FIXTURES / "cs-fastapi-hello"), **extra}


def publish(g: G, sha: str, files: dict[str, bytes] | None = None) -> None:
    """The commit's tarball, as GitHub serves it."""
    g.hub.tarballs[sha] = tarball_of(files or app_files(), f"Acme-ledger-{sha[:7]}")


async def bind(g: G, installation: int | None = None) -> bool:
    return await run_bind(
        g.b.dsn, org=g.b.w.org, operator=OPERATOR, installation=installation or g.installation
    )


def put(g: G, body: dict[str, Any], token: str | None = None, app: str | None = None) -> Response:
    return g.b.client.put(
        f"/v1/apps/{app or g.b.w.app}/github",
        json=body,
        headers=auth(token or g.b.t.builder, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def delete(g: G, token: str | None = None) -> Response:
    return g.b.client.delete(
        f"/v1/apps/{g.b.w.app}/github",
        headers=auth(token or g.b.t.builder, **{IDEMPOTENCY_HEADER: new_key()}),
    )


async def connect(g: G, **body: Any) -> dict[str, Any]:
    await bind(g)
    r = put(g, {"repository": "acme/ledger", **body})
    assert r.status_code == 200, r.text
    return cast("dict[str, Any]", r.json())


def signature(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def deliver(
    g: G,
    event: str,
    payload: object,
    *,
    sig: str | None = None,
    body: bytes | None = None,
) -> Response:
    raw = json.dumps(payload).encode() if body is None else body
    headers = {
        "content-type": "application/json",
        "x-github-event": event,
        "x-github-delivery": str(uuid.uuid4()),
        "x-hub-signature-256": signature(raw) if sig is None else sig,
    }
    if sig == "":
        del headers["x-hub-signature-256"]
    return g.b.client.post("/v1/github/webhook", content=raw, headers=headers)


def push_event(
    g: G,
    sha: str,
    *,
    ref: str = "refs/heads/main",
    repository_id: int | None = None,
    installation: int | None = None,
    deleted: bool = False,
) -> Json:
    return {
        "ref": ref,
        "before": "0" * 40,
        "after": sha,
        "deleted": deleted,
        "repository": {"id": repository_id or g.repo["id"], "full_name": g.repo["full_name"]},
        "installation": {"id": installation or g.installation},
    }


def todo(g: G, sha: str) -> list[dict[str, Any]]:
    return rows_of(
        g.b.dsn,
        g.b.w.org,
        "select args, scheduled_at from procrastinate.procrastinate_jobs "
        "where queueing_lock = %s and status = 'todo'",
        push_lock(g.b.w.app, sha),
    )


def push_jobs(g: G) -> int:
    (row,) = rows_of(
        g.b.dsn,
        g.b.w.org,
        "select count(*) as n from procrastinate.procrastinate_jobs "
        "where task_name = %s and args->>'app_id' = %s",
        RUN_PUSH,
        g.b.w.app,
    )
    return int(row["n"])


async def step(g: G, sha: str) -> str:
    """What the worker does with the waiting push job: fetch it, then run it."""
    (job,) = todo(g, sha)
    take_job(g.b.dsn, push_lock(g.b.w.app, sha))
    return await run_push(g.ports, **job["args"])


def preview_url(g: G) -> str:
    (org,) = rows_of(g.b.dsn, g.b.w.org, "select cell_label from ssc.org where id = %s", g.b.w.org)
    return app_origin("ledger", "preview", org["cell_label"], g.ports.apps_domain)


def promote(g: G, token: str | None = None) -> Response:
    return post(g.b, f"/v1/apps/{g.b.w.app}/promote", {}, token)


def prod_builds(g: G) -> list[dict[str, Any]]:
    return rows_of(
        g.b.dsn, g.b.w.org, "select id from ssc.build where environment_id = %s", g.b.w.prod
    )


async def live_commit(g: G, sha: str | None) -> str:
    """A release built for preview from a bundle a builder uploaded, as the CLI does, declaring
    ``sha``, and healthy there."""
    bundle, _ = seed_bundle(g.b, commit=sha)
    r = start_build(g.b, g.b.w.preview, bundle)
    assert r.status_code == 202, r.text
    build = r.json()["build_id"]
    assert await run_build(g.b.ports, org_id=g.b.w.org, build_id=build) == "succeeded"
    release = str(get(g.b, f"/v1/builds/{build}").json()["release_id"])
    assert (await deploy(g.b, g.b.w.preview, release))[1] == "healthy"
    return release


async def pushed_commit(g: G, sha: str) -> None:
    """``sha`` pushed to the connected branch, built by the push job and healthy in preview."""
    publish(g, sha)
    assert deliver(g, "push", push_event(g, sha)).status_code == 202
    assert await step(g, sha) == "building"
    (job,) = todo(g, sha)
    build = job["args"]["build_id"]
    assert await run_build(g.ports, org_id=g.b.w.org, build_id=build) == "succeeded"
    assert await step(g, sha) == "deploying"
    (dep,) = rows_of(
        g.b.dsn,
        g.b.w.org,
        "select id from ssc.deployment where environment_id = %s and state = 'pending'",
        g.b.w.preview,
    )
    assert await run(g.b, dep["id"], g.ports) == "healthy"
    assert await step(g, sha) == "healthy"


def gets(hub: FakeGitHub, tail: str) -> int:
    """How many GETs the hub answered for a path ending in ``tail``."""
    return sum(1 for c in hub.calls if c.startswith("GET ") and c.endswith(tail))


def set_runtime(g: G, **changes: Any) -> None:
    app = cast("FastAPI", g.b.client.app)
    app.state.runtime = replace(app.state.runtime, **changes)


async def test_a_push_puts_the_preview_address_on_the_commit(g: G) -> None:
    await connect(g)
    sha = sha_of(1)
    publish(g, sha)
    started = time.monotonic()
    r = deliver(g, "push", push_event(g, sha))
    assert (r.status_code, r.json()) == (202, {"status": "queued"})
    assert deliver(g, "push", push_event(g, sha)).json() == {"status": "ignored"}
    assert push_jobs(g) == 1

    assert await step(g, sha) == "building"
    (check,) = g.hub.ssc_runs(sha)
    assert (check["name"], check["status"]) == (CHECK_NAME, "in_progress")
    (bundle,) = rows_of(
        g.b.dsn,
        g.b.w.org,
        "select id, state, source_commit, actor_kind, actor_id from ssc.bundle",
    )
    assert (bundle["state"], bundle["source_commit"]) == ("stored", sha)
    assert (bundle["actor_kind"], bundle["actor_id"]) == ("integration", f"github:{g.installation}")
    (build,) = rows_of(g.b.dsn, g.b.w.org, "select id, environment_id, actor_kind from ssc.build")
    assert (build["environment_id"], build["actor_kind"]) == (g.b.w.preview, "integration")
    (job,) = todo(g, sha)
    assert job["args"]["build_id"] == build["id"]
    assert job["args"]["check_run_id"] == check["id"]
    wait = job["scheduled_at"] - datetime.now(UTC)
    assert timedelta(0) < wait <= timedelta(seconds=10)
    assert any(c.startswith("GET codeload.github.test/") for c in g.hub.calls)

    assert await step(g, sha) == "building"
    assert await run_build(g.ports, org_id=g.b.w.org, build_id=build["id"]) == "succeeded"
    assert await step(g, sha) == "deploying"
    (dep,) = rows_of(
        g.b.dsn, g.b.w.org, "select id, environment_id, actor_kind, kind from ssc.deployment"
    )
    assert (dep["environment_id"], dep["actor_kind"], dep["kind"]) == (
        g.b.w.preview,
        "integration",
        "deploy",
    )
    assert check["status"] == "in_progress"
    assert await step(g, sha) == "deploying"
    assert await run(g.b, dep["id"], g.ports) == "healthy"
    assert await step(g, sha) == "healthy"
    assert time.monotonic() - started < FIVE_MINUTES

    url = preview_url(g)
    assert (check["status"], check["conclusion"], check["details_url"]) == (
        "completed",
        "success",
        url,
    )
    assert url in check["output"]["summary"]
    assert todo(g, sha) == []
    assert prod_builds(g) == []
    actor = f"github:{g.installation}"
    assert [(a["action"], a["actor_kind"], a["actor_id"]) for a in audit_of(g.b, bundle["id"])] == [
        ("bundle.stored", "integration", actor)
    ]
    (started_audit,) = audit_of(g.b, build["id"])[:1]
    assert started_audit["action"] == "build.started"
    assert started_audit["after"]["via"] == "github"
    assert audit_of(g.b, dep["id"])[0]["action"] == "deploy.started"
    for token in g.hub.minted:
        assert token.repository_ids in (None, [g.repo["id"]])
        assert "write" not in {v for k, v in token.permissions.items() if k != "checks"}


async def test_an_older_push_is_never_deployed_over_a_newer_one(g: G) -> None:
    await connect(g)
    old, new = sha_of(2), sha_of(3)
    for sha in (old, new):
        publish(g, sha, app_files(**{"VERSION": sha.encode()}))
        assert deliver(g, "push", push_event(g, sha)).status_code == 202
        assert await step(g, sha) == "building"
    for job in rows_of(g.b.dsn, g.b.w.org, "select id from ssc.build order by created_at"):
        assert await run_build(g.ports, org_id=g.b.w.org, build_id=job["id"]) == "succeeded"
    assert await step(g, old) == "superseded"
    (stale,) = g.hub.ssc_runs(old)
    assert (stale["status"], stale["conclusion"]) == ("completed", "neutral")
    assert await step(g, new) == "deploying"
    assert len(rows_of(g.b.dsn, g.b.w.org, "select id from ssc.deployment")) == 1


async def test_a_redelivered_push_after_the_build_builds_nothing_more(g: G) -> None:
    await connect(g)
    sha = sha_of(4)
    publish(g, sha)
    deliver(g, "push", push_event(g, sha))
    assert await step(g, sha) == "building"
    take_job(g.b.dsn, push_lock(g.b.w.app, sha))
    assert deliver(g, "push", push_event(g, sha)).status_code == 202
    (job,) = [j for j in todo(g, sha) if j["args"]["build_id"] is None]
    assert await run_push(g.ports, **job["args"]) == "duplicate"
    assert len(rows_of(g.b.dsn, g.b.w.org, "select id from ssc.build")) == 1


async def test_a_secret_in_the_pushed_source_fails_the_check_and_builds_nothing(
    g: G, caplog: pytest.LogCaptureFixture
) -> None:
    await connect(g)
    sha = sha_of(5)
    key = supabase_jwt(SERVICE_ROLE_ROLE)
    publish(g, sha, app_files(**{"src/db.js": f'createClient(url, "{key}")\n'.encode()}))
    deliver(g, "push", push_event(g, sha))
    with caplog.at_level(logging.DEBUG):
        assert await step(g, sha) == "failed"
    (check,) = g.hub.ssc_runs(sha)
    assert (check["status"], check["conclusion"]) == ("completed", "failure")
    assert "SECRET_IN_BUNDLE" in check["output"]["summary"]
    assert key not in json.dumps(check) and key[-12:] not in caplog.text
    assert rows_of(g.b.dsn, g.b.w.org, "select id from ssc.build") == []
    assert todo(g, sha) == []


async def test_a_source_github_will_not_hand_over_fails_the_check(g: G) -> None:
    await connect(g)
    sha = sha_of(6)
    deliver(g, "push", push_event(g, sha))
    assert await step(g, sha) == "failed"
    (check,) = g.hub.ssc_runs(sha)
    assert check["conclusion"] == "failure"
    assert "SOURCE_UNAVAILABLE" in check["output"]["summary"]


async def test_a_fork_pull_request_builds_nothing(g: G) -> None:
    await connect(g)
    sha = sha_of(7)
    publish(g, sha)
    fork = {"id": 99, "full_name": "mallory/ledger", "fork": True}
    pull = {
        "action": "opened",
        "number": 7,
        "pull_request": {
            "head": {"sha": sha, "ref": "main", "repo": fork},
            "base": {"ref": "main", "repo": {"id": g.repo["id"]}},
        },
        "repository": {"id": g.repo["id"], "full_name": g.repo["full_name"]},
        "installation": {"id": g.installation},
    }
    deliveries: list[tuple[str, object]] = [
        ("pull_request", pull),
        ("pull_request", {**pull, "action": "synchronize"}),
        ("pull_request_target", pull),
        ("push", push_event(g, sha, repository_id=99)),
        ("push", push_event(g, sha, installation=g.installation + 1)),
        ("push", push_event(g, sha, ref="refs/heads/feature")),
        ("push", push_event(g, sha, ref="refs/tags/v1")),
        ("push", push_event(g, sha, deleted=True)),
        ("push", push_event(g, "0" * 40)),
        ("ping", {"zen": "Keep it logically awesome.", "hook_id": 1}),
    ]
    for event, payload in deliveries:
        r = deliver(g, event, payload)
        assert (r.status_code, r.json()) == (200, {"status": "ignored"}), event
    assert push_jobs(g) == 0
    assert rows_of(g.b.dsn, g.b.w.org, "select id from ssc.bundle") == []
    assert g.hub.check_runs == {}
    assert not any("tarball" in c or "codeload" in c for c in g.hub.calls)


async def test_a_forged_webhook_is_refused(g: G) -> None:
    await connect(g)
    sha = sha_of(8)
    body = json.dumps(push_event(g, sha)).encode()
    forged = [
        deliver(g, "push", None, body=body, sig=signature(body, b"not-the-secret")),
        deliver(g, "push", None, body=body, sig=""),
        deliver(g, "push", None, body=body, sig="sha1=" + hashlib.sha1(body).hexdigest()),  # noqa: S324  (a forger's header)
        deliver(g, "push", None, body=body, sig=signature(body).upper()),
        deliver(g, "push", None, body=body.replace(b"main", b"mainx"), sig=signature(body)),
        deliver(g, "push", None, body=body + b" ", sig=signature(body)),
    ]
    for r in forged:
        assert_problem(r, ErrorCode.UNAUTHENTICATED)
    big = b"{" + b" " * MAX_BODY_BYTES + b"}"
    assert_problem(
        deliver(g, "push", None, body=big, sig=signature(big)), ErrorCode.UNAUTHENTICATED
    )
    assert push_jobs(g) == 0
    r = deliver(g, "push", None, body=b"{not json", sig=signature(b"{not json"))
    assert_problem(r, ErrorCode.VALIDATION_FAILED)
    assert deliver(g, "push", None, body=body).status_code == 202


async def test_without_a_secret_every_delivery_is_refused(g: G) -> None:
    await connect(g)
    app = cast("FastAPI", g.b.client.app)
    set_runtime(g, settings=replace(app.state.runtime.settings, github_webhook_secret=None))
    body = json.dumps(push_event(g, sha_of(9))).encode()
    for sig in (signature(body), signature(body, b""), ""):
        assert_problem(deliver(g, "push", None, body=body, sig=sig), ErrorCode.UNAUTHENTICATED)
    assert push_jobs(g) == 0


def test_the_webhook_is_not_in_the_openapi_document(g: G) -> None:
    paths = g.b.client.get("/openapi.json").json()["paths"]
    assert "/v1/github/webhook" not in paths
    assert "/v1/apps/{app_id}/github" in paths


async def test_promote_is_refused_while_a_required_check_is_red(g: G) -> None:
    await connect(g, required_checks=[TEST])
    sha = sha_of(10)
    await pushed_commit(g, sha)
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    g.hub.ci(sha, "test", CI, "main", "failure")
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    g.hub.ci(sha, "test", ".github/workflows/other.yml", "main")
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    g.hub.ci(sha, "test", CI, "feature")
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    g.hub.ci(sha, "lint", CI, "main")
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    g.hub.ci(sha, "test", CI, "main")
    g.hub.ci(sha, "test", CI, "main", None, status="in_progress")
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    assert prod_builds(g) == []
    g.hub.ci(sha, "test", f"{CI}@refs/heads/main", "main")
    r = promote(g)
    assert r.status_code == 202, r.text
    assert [b["id"] for b in prod_builds(g)] == [r.json()["build_id"]]


async def test_a_release_without_a_commit_cannot_pass_the_gate(g: G) -> None:
    await connect(g, required_checks=[TEST])
    await live_commit(g, None)
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    assert prod_builds(g) == []


async def test_an_uploaded_bundle_declaring_a_green_commit_cannot_pass_the_gate(
    g: G, caplog: pytest.LogCaptureFixture
) -> None:
    await connect(g, required_checks=[TEST])
    sha = sha_of(12)
    g.hub.ci(sha, "test", CI, "main")
    await live_commit(g, sha)
    with caplog.at_level(logging.WARNING, logger="ssc.api"):
        r = promote(g)
    assert_problem(r, ErrorCode.REQUIRED_CHECKS_FAILING)
    assert logged_evidence(caplog, r) == {"reason": "not_from_github"}
    assert prod_builds(g) == []
    await pushed_commit(g, sha)
    r = promote(g)
    assert r.status_code == 202, r.text


async def test_the_gate_never_opens_without_github(g: G) -> None:
    await connect(g, required_checks=[TEST])
    sha = sha_of(11)
    await pushed_commit(g, sha)
    g.hub.ci(sha, "test", CI, "main")
    g.hub.fail = 502
    assert_problem(promote(g), ErrorCode.GITHUB_UNAVAILABLE)
    g.hub.fail = None
    set_runtime(g, github=None)
    assert_problem(promote(g), ErrorCode.GITHUB_UNAVAILABLE)
    assert prod_builds(g) == []


async def test_the_gate_reads_a_required_check_past_the_first_page(g: G) -> None:
    await connect(g, required_checks=[TEST])
    sha = sha_of(15)
    await pushed_commit(g, sha)
    for i in range(150):
        g.hub.ci(sha, f"other-{i}", CI, "main")
    g.hub.ci(sha, "test", CI, "main")
    assert len(g.hub.check_runs[sha]) > PAGE and len(g.hub.workflow_runs[sha]) > PAGE
    g.hub.calls.clear()
    r = promote(g)
    assert r.status_code == 202, r.text
    assert (gets(g.hub, "/check-runs"), gets(g.hub, "/actions/runs")) == (2, 2)


async def test_the_latest_run_wins_across_pages(g: G) -> None:
    await connect(g, required_checks=[TEST])
    sha = sha_of(16)
    await pushed_commit(g, sha)
    green = g.hub.ci(sha, "test", CI, "main")
    for i in range(120):
        g.hub.ci(sha, f"other-{i}", CI, "main")
    red = g.hub.ci(sha, "test", CI, "main", "failure")
    assert red["id"] > green["id"] and g.hub.check_runs[sha].index(red) >= PAGE
    assert_problem(promote(g), ErrorCode.REQUIRED_CHECKS_FAILING)
    assert prod_builds(g) == []


async def test_too_many_check_runs_fails_closed(g: G, caplog: pytest.LogCaptureFixture) -> None:
    await connect(g, required_checks=[TEST])
    sha = sha_of(17)
    await pushed_commit(g, sha)
    for i in range(MAX_PAGES * PAGE):
        g.hub.ci(sha, f"other-{i}", CI, "main")
    g.hub.ci(sha, "test", CI, "main")
    g.hub.calls.clear()
    with caplog.at_level(logging.WARNING, logger="ssc.api"):
        r = promote(g)
    assert_problem(r, ErrorCode.GITHUB_UNAVAILABLE)
    assert logged_evidence(caplog, r) == {"reason": "too_many_results", "path": "checks"}
    assert (gets(g.hub, "/check-runs"), gets(g.hub, "/actions/runs")) == (MAX_PAGES, 0)
    assert prod_builds(g) == []


async def test_a_small_commit_is_one_request_per_list(g: G) -> None:
    await connect(g, required_checks=[TEST])
    sha = sha_of(18)
    await pushed_commit(g, sha)
    for i in range(4):
        g.hub.ci(sha, f"other-{i}", CI, "main")
    g.hub.ci(sha, "test", CI, "main")
    g.hub.calls.clear()
    r = promote(g)
    assert r.status_code == 202, r.text
    assert (gets(g.hub, "/check-runs"), gets(g.hub, "/actions/runs")) == (1, 1)


async def test_no_required_checks_or_no_connection_leaves_promote_as_it_was(g: G) -> None:
    await connect(g)
    await live_commit(g, None)
    g.hub.fail = 502
    r = promote(g)
    assert r.status_code == 202, r.text


async def test_connecting_and_disconnecting_are_audited_per_app(g: G) -> None:
    await bind(g)
    r = put(g, {"repository": "acme/ledger", "required_checks": [TEST]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["repository"] == "Acme/ledger"
    assert (out["repository_id"], out["branch"], out["check_name"]) == (
        g.repo["id"],
        "main",
        CHECK_NAME,
    )
    assert out["required_checks"] == [TEST]
    assert get(g.b, f"/v1/apps/{g.b.w.app}/github").json() == out
    assert put(g, {"repository": "acme/ledger", "required_checks": [TEST]}).status_code == 200
    r = put(g, {"repository": "acme/ledger", "branch": "release/1"})
    assert r.json()["branch"] == "release/1"
    assert delete(g).status_code == 204
    assert_problem(get(g.b, f"/v1/apps/{g.b.w.app}/github"), ErrorCode.NOT_FOUND)
    assert_problem(delete(g), ErrorCode.NOT_FOUND)

    events = audit_of(g.b, g.b.w.app)
    linked = [e for e in events if e["action"].startswith("repo.")]
    assert [(e["action"], e["actor_id"]) for e in linked] == [
        ("repo.connected", g.b.w.builder),
        ("repo.connected", g.b.w.builder),
        ("repo.disconnected", g.b.w.builder),
    ]
    view = {"installation_id": g.installation, "repository_id": g.repo["id"]}
    assert linked[0]["before"] is None
    assert linked[0]["after"] == {**view, "branch": "main", "required_checks": [f"{CI}:test"]}
    assert linked[1]["after"] == {**view, "branch": "release/1", "required_checks": []}
    assert linked[2]["before"] == linked[1]["after"]
    assert "ledger" not in json.dumps([e["before"] for e in linked] + [e["after"] for e in linked])


async def test_connecting_needs_a_bound_installation_and_a_builder_on_prod(
    g: G, signing_key: SigningKey
) -> None:
    body = {"repository": "acme/ledger"}
    assert_problem(put(g, body), ErrorCode.REPOSITORY_NOT_INSTALLED)
    await bind(g)
    g.hub.repo("acme/elsewhere", g.installation + 1)
    assert_problem(put(g, {"repository": "acme/elsewhere"}), ErrorCode.REPOSITORY_NOT_INSTALLED)
    assert_problem(put(g, {"repository": "acme/missing"}), ErrorCode.REPOSITORY_NOT_INSTALLED)
    assert_problem(put(g, body, g.b.t.member), ErrorCode.FORBIDDEN)
    scoped = mint(
        signing_key, org=g.b.w.org, sub=g.b.w.admin, jti=f"cred_{new_key()[:16]}", scope="preview"
    )
    assert_problem(put(g, body, scoped), ErrorCode.FORBIDDEN)
    agent = mint(
        signing_key, org=g.b.w.org, sub=g.b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True
    )
    assert_problem(put(g, body, agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert_problem(put(g, body, app=new_id("app")), ErrorCode.NOT_FOUND)
    bad = {"repository": "acme/ledger", "required_checks": [{"name": "t", "workflow": "ci.yml"}]}
    assert_problem(put(g, bad), ErrorCode.VALIDATION_FAILED)
    assert_problem(put(g, {"repository": "../etc"}), ErrorCode.VALIDATION_FAILED)
    assert_problem(get(g.b, f"/v1/apps/{g.b.w.app}/github", g.b.t.member), ErrorCode.FORBIDDEN)
    assert put(g, body).status_code == 200
    assert_problem(delete(g, g.b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(delete(g, scoped), ErrorCode.FORBIDDEN)
    assert_problem(delete(g, agent), ErrorCode.AGENT_SESSION_REFUSED)
    g.hub.fail = 500
    assert_problem(put(g, body), ErrorCode.GITHUB_UNAVAILABLE)
    g.hub.fail = None
    set_runtime(g, github=None)
    assert_problem(put(g, body), ErrorCode.GITHUB_UNAVAILABLE)


async def test_a_disconnected_app_stops_building_pushes(g: G) -> None:
    await connect(g)
    sha = sha_of(12)
    publish(g, sha)
    assert deliver(g, "push", push_event(g, sha)).status_code == 202
    assert delete(g).status_code == 204
    assert await step(g, sha) == "disconnected"
    assert deliver(g, "push", push_event(g, sha_of(13))).json() == {"status": "ignored"}
    assert g.hub.check_runs == {}


async def test_a_disabled_app_reports_on_the_commit_and_builds_nothing(g: G) -> None:
    await connect(g)
    sha = sha_of(14)
    publish(g, sha)
    deliver(g, "push", push_event(g, sha))
    execute(g.b.dsn, g.b.w.org, "update ssc.app set status = 'disabled' where id = %s", g.b.w.app)
    assert await step(g, sha) == "app_not_active"
    (check,) = g.hub.ssc_runs(sha)
    assert "APP_NOT_ACTIVE" in check["output"]["summary"]
    assert rows_of(g.b.dsn, g.b.w.org, "select id from ssc.bundle") == []


async def test_binding_an_installation(g: G, dsns: Dsns, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await bind(g) is True
    assert await bind(g) is False
    (event,) = audit_of(g.b, str(g.installation))
    assert (event["action"], event["actor_kind"], event["actor_id"]) == (
        "github.installation_bound",
        "operator",
        OPERATOR,
    )
    engine = make_engine(dsns.app)
    try:
        other = await create_org(
            engine, NewOrg("Elsewhere", "Bo", "bo@example.com", ISSUER, new_id("usr"))
        )
    finally:
        await engine.dispose()
    with pytest.raises(BindError):
        await run_bind(dsns.app, org=other.org_id, operator=OPERATOR, installation=g.installation)

    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    argv = [
        "bind",
        "--org",
        g.b.w.org,
        "--operator",
        OPERATOR,
        "--installation",
        str(g.installation + 1),
    ]
    assert await asyncio.to_thread(main, argv) == 0
    assert await asyncio.to_thread(main, argv) == 0
    clash = [
        "bind",
        "--org",
        other.org_id,
        "--operator",
        OPERATOR,
        "--installation",
        str(g.installation + 1),
    ]
    assert await asyncio.to_thread(main, clash) == 1
    with pytest.raises(SystemExit):
        main(["bind", "--org", g.b.w.org, "--operator", OPERATOR, "--installation", "0"])
    monkeypatch.delenv("SSC_DATABASE_DSN")
    assert main(argv) == 2


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.at


async def test_installation_tokens_are_cached_and_scoped() -> None:
    hub, clock = FakeGitHub(), Clock()
    repo = hub.repo("acme/ledger", INSTALLATION)
    github = hub.client(clock=clock)
    try:
        first = await github.installation_token(INSTALLATION, {"contents": "read"}, repo["id"])
        assert await github.installation_token(INSTALLATION, {"contents": "read"}, repo["id"]) == (
            first
        )
        assert len(hub.minted) == 1
        assert hub.minted[0].repository_ids == [repo["id"]]
        assert hub.minted[0].permissions == {"contents": "read"}
        await github.installation_token(INSTALLATION, {"checks": "write"}, repo["id"])
        assert len(hub.minted) == 2
        clock.at += timedelta(minutes=51)
        assert (
            await github.installation_token(INSTALLATION, {"contents": "read"}, repo["id"]) != first
        )
        assert len(hub.minted) == 3
        assert "PRIVATE" not in repr(github) and APP_ID in repr(github)
    finally:
        await github.aclose()


async def test_paging_stops_on_an_empty_short_or_counted_page() -> None:
    hub = FakeGitHub()
    raw = hub.repo("acme/ledger", INSTALLATION)
    repo = RepoRef(installation_id=INSTALLATION, id=raw["id"], name="acme/ledger")
    github = hub.client()
    try:
        for n, runs, requests in [
            (20, 0, 1),
            (21, 150, 2),
            (22, 2 * PAGE, 2),
            (23, MAX_PAGES * PAGE, MAX_PAGES),
        ]:
            sha = sha_of(n)
            for i in range(runs):
                hub.ci(sha, f"check-{i}", CI, "main")
            hub.calls.clear()
            got = await github.check_runs(repo, sha)
            assert [r["id"] for r in got] == [r["id"] for r in hub.check_runs.get(sha, [])]
            assert gets(hub, f"/commits/{sha}/check-runs") == requests, runs
    finally:
        await github.aclose()


def test_the_app_jwt_lasts_under_ten_minutes_and_names_the_app() -> None:
    hub, clock = FakeGitHub(), Clock()
    github = hub.client(clock=clock)
    claims = jwt.decode(
        github.app_jwt(), _PUBLIC, algorithms=["RS256"], options={"verify_exp": False}
    )
    now = int(clock.at.timestamp())
    assert claims["iss"] == APP_ID
    assert claims["iat"] < now < claims["exp"] <= now + 600
    asyncio.run(github.aclose())


async def test_the_worker_says_whether_github_is_ready(caplog: pytest.LogCaptureFixture) -> None:
    """GA-7.2: the worker logs ``github ready`` once the App's id and key are set, a warning
    when neither is, and never the key. Composing the App calls nothing."""
    with caplog.at_level(logging.INFO, logger="ssc_control.worker"):
        assert github_from_env({}) is None
    assert "SSC_GITHUB_APP_ID is not set: GitHub push jobs will do nothing" in caplog.text
    assert "github ready" not in caplog.text
    caplog.clear()
    env = {"SSC_GITHUB_APP_ID": APP_ID, "SSC_GITHUB_PRIVATE_KEY": _PEM, "SSC_GITHUB_API_BASE": BASE}
    with caplog.at_level(logging.INFO, logger="ssc_control.worker"):
        github = github_from_env(env)
    assert github is not None
    try:
        (ready,) = [r for r in caplog.records if r.getMessage() == "github ready"]
        assert ready.levelno == logging.INFO
        assert ready.__dict__["app_id"] == APP_ID
        assert "is not set" not in caplog.text
        assert _PEM.splitlines()[1] not in caplog.text and _PEM[-40:] not in caplog.text
    finally:
        await github.aclose()
    for alone in ({"SSC_GITHUB_APP_ID": APP_ID}, {"SSC_GITHUB_PRIVATE_KEY": _PEM}):
        with pytest.raises(CompositionError, match="set both"):
            github_from_env(alone)


def test_the_gate_binds_a_check_to_its_workflow_and_branch() -> None:
    required = (RequiredCheck("test", CI), RequiredCheck("lint", CI))
    suites = [
        {"check_suite_id": 1, "path": CI, "head_branch": "main"},
        {"check_suite_id": 2, "path": f"{CI}@refs/heads/main", "head_branch": "main"},
        {"check_suite_id": 3, "path": ".github/workflows/x.yml", "head_branch": "main"},
    ]

    def check(run_id: int, name: str, suite: int, conclusion: str = "success") -> Json:
        return {
            "id": run_id,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "check_suite": {"id": suite},
        }

    assert gate.failing(required, "main", [], suites) == list(required)
    runs = [check(1, "test", 1), check(2, "lint", 3)]
    assert gate.failing(required, "main", runs, suites) == [required[1]]
    runs.append(check(3, "lint", 2))
    assert gate.failing(required, "main", runs, suites) == []
    assert gate.failing(required, "feature", runs, suites) == list(required)
    runs.append(check(4, "test", 1, "cancelled"))
    assert gate.failing(required, "main", runs, suites) == [required[0]]


def _tar(entries: list[tarfile.TarInfo], data: dict[str, bytes] | None = None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for info in entries:
            body = (data or {}).get(info.name)
            if body is not None:
                info.size = len(body)
            tar.addfile(info, None if body is None else io.BytesIO(body))
    return raw.getvalue()


def _entry(name: str, kind: bytes = tarfile.REGTYPE, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    return info


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ([_entry("top/../../etc/passwd")], "unsafe_path"),
        ([_entry("/etc/passwd")], "absolute_path"),
        ([_entry("top/a"), _entry("other/b")], "two_roots"),
        ([_entry("top/link", tarfile.SYMTYPE, "/etc/passwd")], "link"),
        ([_entry("top/hard", tarfile.LNKTYPE, "top/a")], "link"),
        ([_entry("top/fifo", tarfile.FIFOTYPE)], "special_file"),
        ([_entry("top/dev", tarfile.CHRTYPE)], "special_file"),
        ([_entry("top/a"), _entry("top/a")], "case_collision"),
    ],
)
def test_the_unpacker_refuses_unsafe_sources(
    tmp_path: Path, entries: list[tarfile.TarInfo], reason: str
) -> None:
    tarball = tmp_path / "source.tar.gz"
    tarball.write_bytes(_tar(entries, {e.name: b"x" for e in entries if e.isfile()}))
    dest = tmp_path / "tree"
    dest.mkdir()
    with pytest.raises(BundleMalformedError) as caught:
        unpack(tarball, dest, Limits())
    assert caught.value.reason == reason
    assert not (tmp_path / "etc").exists()


def test_the_unpacker_stops_at_the_bundle_limits(tmp_path: Path) -> None:
    tarball = tmp_path / "source.tar.gz"
    tarball.write_bytes(tarball_of({"a": b"12345", "b": b"12345"}, "top"))
    for limits in (Limits(max_files=1), Limits(max_unpacked_bytes=6)):
        dest = tmp_path / f"tree-{limits.max_files}"
        dest.mkdir()
        with pytest.raises(BundleTooLargeError):
            unpack(tarball, dest, limits)
    tarball.write_bytes(b"not a tarball")
    with pytest.raises(BundleMalformedError) as caught:
        unpack(tarball, tmp_path / "tree-x", Limits())
    assert caught.value.reason == "not_tar_gz"


def test_the_unpacker_drops_the_top_folder(tmp_path: Path) -> None:
    tarball = tmp_path / "source.tar.gz"
    tarball.write_bytes(tarball_of({"ssc.toml": MANIFEST, "src/app.py": b"print(1)\n"}, "o-r-1"))
    dest = tmp_path / "tree"
    dest.mkdir()
    unpack(tarball, dest, Limits())
    assert files_of(dest) == {"ssc.toml": MANIFEST, "src/app.py": b"print(1)\n"}
