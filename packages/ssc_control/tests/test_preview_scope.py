"""D3 for SSC-042: a credential with ``scope: preview`` never touches production. It may change
preview, upload source and read, and nothing else. Checked in the unit of work, before any
handler runs."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, make_org, mint, new_key

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.api.auth import CredentialScope, principal_from_claims
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.problems import REQUEST_ID_HEADER, Refusal
from ssc_control.api.uow import PREVIEW_SCOPE_CHANGES
from ssc_control.db import CreatedOrg, bind_org_sync


@dataclass(frozen=True)
class Bench:
    dsns: Dsns
    client: TestClient
    org: CreatedOrg
    full: str
    preview: str
    app: dict[str, Any]

    def env(self, name: str) -> str:
        return next(e["id"] for e in self.app["environments"] if e["name"] == name)


@pytest.fixture(scope="module")
def b(
    dsns: Dsns, signing_key: SigningKey, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Bench]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        environment="test",
        blob_backend="fs",
        blob_root=str(tmp_path_factory.mktemp("blobs")),
        blob_signing_keys={"k1": b"k" * 32},
        blob_signing_kid="k1",
    )
    org = make_org(dsns.app, "Scoped")
    full = mint(signing_key, org=org.org_id, sub=org.admin_user_id)
    preview = mint(signing_key, org=org.org_id, sub=org.admin_user_id, scope="preview")
    with TestClient(create_app(settings)) as client:
        r = client.post(
            "/v1/apps",
            json={"slug": "ci-app"},
            headers=auth(full, **{IDEMPOTENCY_HEADER: new_key()}),
        )
        assert r.status_code == 201, r.text
        yield Bench(dsns, client, org, full, preview, r.json())


def post(b: Bench, path: str, body: dict[str, Any] | None = None) -> Response:
    return b.client.post(
        path, json=body, headers=auth(b.preview, **{IDEMPOTENCY_HEADER: new_key()})
    )


def put_grants(b: Bench, env: str, token: str, grants: list[dict[str, Any]]) -> Response:
    url = f"/v1/apps/{b.app['id']}/environments/{b.env(env)}/grants"
    current = b.client.get(url, headers=auth(b.full))
    return b.client.put(
        url, json={"grants": grants}, headers=auth(token, **{"If-Match": current.headers["ETag"]})
    )


def add_release(b: Bench) -> str:
    rid = new_id("rel")
    d = "sha256:" + hashlib.sha256(rid.encode()).hexdigest()
    with psycopg.connect(b.dsns.app) as conn:
        bind_org_sync(conn, b.org.org_id)
        conn.execute(
            "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
            "source_digest, actor_kind, actor_id) values (%s, %s, %s, "
            "(select coalesce(max(number), 0) + 1 from ssc.release where app_id = %s), "
            "%s, %s, %s, 'user', %s)",
            (rid, b.org.org_id, b.app["id"], b.app["id"], d, d, d, b.org.admin_user_id),
        )
    return rid


def count(b: Bench, sql: str, *args: object) -> int:
    with psycopg.connect(b.dsns.app) as conn:
        bind_org_sync(conn, b.org.org_id)
        row = conn.execute(sql, args).fetchone()
        assert row is not None
        return int(row[0])


def refused_by_scope(caplog: pytest.LogCaptureFixture, r: Response) -> None:
    assert_problem(r, ErrorCode.FORBIDDEN)
    rid = r.headers[REQUEST_ID_HEADER]
    lines = [
        rec.getMessage()[8:] for rec in caplog.records if rec.getMessage().startswith("refusal ")
    ]
    evidence = [json.loads(x)["evidence"] for x in lines if json.loads(x)["request_id"] == rid]
    assert evidence and evidence[0]["reason"] == "preview_scope", evidence


# ── the claim ────────────────────────────────────────────────────────────────

_CLAIMS = {"org": "org_" + "a" * 20, "kind": "user", "sub": "usr_x", "jti": "cred_x"}


def test_the_scope_claim_is_read() -> None:
    assert principal_from_claims(_CLAIMS).scope is None
    assert principal_from_claims({**_CLAIMS, "scope": None}).scope is None
    assert principal_from_claims({**_CLAIMS, "scope": "preview"}).scope is CredentialScope.PREVIEW


@pytest.mark.parametrize("scope", ["prod", "PREVIEW", "", "openid profile", ["preview"], 1])
def test_any_other_scope_is_refused(scope: object) -> None:
    with pytest.raises(Refusal) as e:
        principal_from_claims({**_CLAIMS, "scope": scope})
    assert e.value.code is ErrorCode.UNAUTHENTICATED


def test_an_unknown_scope_is_unauthenticated_on_the_wire(b: Bench, signing_key: SigningKey) -> None:
    token = mint(signing_key, org=b.org.org_id, sub=b.org.admin_user_id, scope="admin")
    assert_problem(b.client.get("/v1/apps", headers=auth(token)), ErrorCode.UNAUTHENTICATED)


# ── production is out of reach ───────────────────────────────────────────────


def test_preview_scoped_token_cannot_change_prod(
    b: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    grants = [{"role": "user", "subject_kind": "org"}]
    release = add_release(b)
    deployments = f"/v1/apps/{b.app['id']}/environments/{b.env('prod')}/deployments"
    builds = f"/v1/apps/{b.app['id']}/environments/{b.env('prod')}/builds"
    with caplog.at_level(logging.WARNING, logger="ssc.api"):
        refused_by_scope(caplog, put_grants(b, "prod", b.preview, grants))
        refused_by_scope(caplog, post(b, builds, {"bundle_id": new_id("bdl")}))
        refused_by_scope(caplog, post(b, deployments, {"release_id": release}))
        refused_by_scope(caplog, post(b, deployments, {"release_id": release, "kind": "rollback"}))
    assert count(b, "select grants_version from ssc.environment where id = %s", b.env("prod")) == 1
    for table in ("build", "deployment"):
        sql = f"select count(*) from ssc.{table} where environment_id = %s"
        assert count(b, sql, b.env("prod")) == 0, table
    assert (
        count(b, "select count(*) from ssc.audit_event where action ~ '^(grant|deploy|build)[.]'")
        == 0
    )
    assert put_grants(b, "prod", b.full, grants).status_code == 200


def test_preview_scoped_token_cannot_read_prod(b: Bench, caplog: pytest.LogCaptureFixture) -> None:
    base = f"/v1/apps/{b.app['id']}/environments/{b.env('prod')}"
    with caplog.at_level(logging.WARNING, logger="ssc.api"):
        for tail in ("/grants", "/access"):
            refused_by_scope(caplog, b.client.get(base + tail, headers=auth(b.preview)))


def test_preview_scoped_token_cannot_make_changes_naming_no_environment(
    b: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """The rule C3b's promote endpoint will fall under too: it names no environment."""
    ask = {"environment_id": b.env("preview"), "kind": "connect_data_source", "subject_key": "db"}
    with caplog.at_level(logging.WARNING, logger="ssc.api"):
        refused_by_scope(caplog, post(b, "/v1/apps", {"slug": "from-ci"}))
        refused_by_scope(caplog, post(b, "/v1/approvals", ask))
    assert count(b, "select count(*) from ssc.app where slug = 'from-ci'") == 0
    assert count(b, "select count(*) from ssc.approval_request") == 0


# ── preview, source and reads stay open ──────────────────────────────────────


def test_preview_scoped_token_works_in_preview(b: Bench) -> None:
    grants = [{"role": "builder", "subject_kind": "user", "subject_id": b.org.admin_user_id}]
    assert put_grants(b, "preview", b.preview, grants).status_code == 200
    url = f"/v1/apps/{b.app['id']}/environments/{b.env('preview')}/deployments"
    r = post(b, url, {"release_id": add_release(b)})
    assert r.status_code == 202, r.text
    assert b.client.get(r.headers["Location"], headers=auth(b.preview)).status_code == 200


def test_preview_scoped_token_may_build_in_preview(b: Bench) -> None:
    url = f"/v1/apps/{b.app['id']}/environments/{b.env('preview')}/builds"
    assert_problem(post(b, url, {"bundle_id": new_id("bdl")}), ErrorCode.REFERENCE_NOT_FOUND)


def test_preview_scoped_token_may_upload_source(b: Bench) -> None:
    data = b"not really a bundle"
    body = {"digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    created = post(b, f"/v1/apps/{b.app['id']}/bundles", body)
    assert created.status_code == 201, created.text
    missing = post(b, f"/v1/apps/{b.app['id']}/bundles/{new_id('bdl')}/complete")
    assert_problem(missing, ErrorCode.NOT_FOUND)


def test_preview_scoped_token_may_read(b: Bench) -> None:
    for path in ("/v1/whoami", "/v1/apps", f"/v1/apps/{b.app['id']}"):
        assert b.client.get(path, headers=auth(b.preview)).status_code == 200, path


def test_an_unknown_environment_is_still_not_found(b: Bench) -> None:
    url = f"/v1/apps/{b.app['id']}/environments/{new_id('env')}/grants"
    r = b.client.put(url, json={"grants": []}, headers=auth(b.preview, **{"If-Match": '"1"'}))
    assert_problem(r, ErrorCode.NOT_FOUND)


def test_the_allowed_changes_are_real_routes(b: Bench) -> None:
    paths = b.client.app.openapi()["paths"]  # type: ignore[attr-defined]
    for change in PREVIEW_SCOPE_CHANGES:
        method, path = change.split(" ")
        assert method.lower() in paths[path], change
