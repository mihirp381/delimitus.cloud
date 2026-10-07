"""SSC-014: source bundles through the API, the filesystem store's signed URLs, and the stored
manifest a release reads, against postgres:18.

Ticket "done when" checks:
  * the same digest twice is one bundle and one object -> test_the_same_digest_is_one_bundle_...
  * upload URLs expire after 10 minutes              -> test_upload_urls_expire_after_ten_...
  * a service_role key in the bundle stops it         -> test_a_service_role_key_is_a_secret_...
  * .env and oversized bundles are refused            -> test_an_env_file_is_malformed,
                                                         test_*_over_the_*_cap_*
Plus: who may ship, a disabled app, repeat completes, the manifest read from the bundle, the
fs-store guard, and ``BundleReleaseSpecs``. B6: a collected upload is asked for again, and
recording or completing waits for the collector (test_bundle_gc has the collector itself).
Secret-shaped values are built at runtime (gitleaks).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import logging
import tarfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from psycopg.rows import dict_row
from ssc_testkit import (
    ISSUER,
    Dsns,
    SigningKey,
    assert_problem,
    auth,
    mint,
    new_key,
    wait_for_a_lock_wait,
)

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import Manifest, load_manifest
from ssc_control import storage
from ssc_control.api import Settings, create_app
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.routes.blobs import blob_store_for, cell_stores_for
from ssc_control.db import NewOrg, bind_org_sync, bound_org, create_org, make_engine
from ssc_control.deploy.bundle_gc import GRACE, LOCK_CLASS, Collected, collect_org
from ssc_control.deploy.bundles import bundle_key
from ssc_control.runtime.specs import (
    BundleReleaseSpecs,
    ReleaseSpecUnavailableError,
    release_manifest,
)
from ssc_control.storage import StorageConfigError, blob_store_from_env
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.canonical import manifest_digest

SIGNING_KEYS = {"k1": b"k" * 32}
MANIFEST = b'schema = "ssc/v1"\n'
SERVICE_ROLE_ROLE = "service" + "_role"

# ── world ────────────────────────────────────────────────────────────────────


class ManualClock:
    def __init__(self) -> None:
        self.at = datetime(2026, 9, 29, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at


@dataclass(frozen=True)
class World:
    org: str
    admin: str  # the org's first admin and the app's owner
    member: str  # an active member with no grant
    builder: str  # an active member with a builder grant on prod
    app: str
    prod: str


@dataclass(frozen=True)
class Tokens:
    admin: str
    member: str
    builder: str


@dataclass(frozen=True)
class Bench:
    client: TestClient
    store: FsBlobStore
    clock: ManualClock
    w: World
    t: Tokens
    dsn: str


def add_member(conn: psycopg.Connection[Any], org: str) -> str:
    uid = new_id("usr")
    conn.execute(
        "insert into ssc.user_account (id, org_id, display_name, email, role, status) "
        "values (%s, %s, 'Some One', 'someone@example.com', 'member', 'active')",
        (uid, org),
    )
    return uid


async def make_world(dsn: str) -> World:
    engine = make_engine(dsn)
    try:
        spec = NewOrg("Bundles", "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        created = await create_org(engine, spec)
    finally:
        await engine.dispose()
    org, admin = created.org_id, created.admin_user_id
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        member, builder = add_member(conn, org), add_member(conn, org)
        app, prod = new_id("app"), new_id("env")
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'ledger', %s)",
            (app, org, admin),
        )
        conn.execute(
            "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, 'prod')",
            (prod, org, app),
        )
        conn.execute(
            "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, "
            "granted_by_user_id) values (%s, %s, %s, 'builder', 'user', %s, %s)",
            (new_id("gnt"), org, prod, builder, admin),
        )
    return World(org, admin, member, builder, app, prod)


def settings_for(dsns: Dsns, signing_key: SigningKey, **overrides: Any) -> Settings:
    base = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        environment="test",
    )
    return replace(base, **overrides)


@pytest.fixture
def world(dsns: Dsns) -> World:
    return asyncio.run(make_world(dsns.app))


@pytest.fixture
def tokens(world: World, signing_key: SigningKey) -> Tokens:
    def token(sub: str) -> str:
        return mint(signing_key, org=world.org, sub=sub, jti=f"cred_{new_key()[:16]}")

    return Tokens(
        admin=token(world.admin), member=token(world.member), builder=token(world.builder)
    )


@pytest.fixture
def make_bench(
    dsns: Dsns, signing_key: SigningKey, world: World, tokens: Tokens, tmp_path: Path
) -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(**overrides: Any) -> Bench:
        clock = ManualClock()
        store = FsBlobStore(
            tmp_path / f"blobs{len(clients)}",
            signer=UrlSigner(SIGNING_KEYS, active="k1", clock=clock),
            base_url="http://testserver/blobs",
            clock=clock,
        )
        client = TestClient(create_app(settings_for(dsns, signing_key, **overrides), None, store))
        client.__enter__()
        clients.append(client)
        return Bench(client, store, clock, world, tokens, dsns.app)

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def b(make_bench: Any) -> Bench:
    return make_bench()


# ── helpers ──────────────────────────────────────────────────────────────────


def tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), mtime=0)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def b64(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def supabase_jwt(role: str) -> str:
    header = b64({"alg": "HS256", "typ": "JWT"})
    return f"{header}.{b64({'iss': 'supabase', 'role': role})}." + "Xk2" * 12


def create(
    b: Bench,
    data: bytes,
    token: str | None = None,
    *,
    size: int | None = None,
    app: str | None = None,
    commit: str | None = None,
) -> Response:
    body: dict[str, Any] = {"digest": sha(data), "size_bytes": len(data) if size is None else size}
    if commit is not None:
        body["source_commit"] = commit
    return b.client.post(
        f"/v1/apps/{app or b.w.app}/bundles",
        json=body,
        headers=auth(token or b.t.builder, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def upload(b: Bench, target: dict[str, Any], data: bytes) -> Response:
    return b.client.put(target["url"], content=data, headers=target["headers"])


def complete(b: Bench, bundle_id: str, token: str | None = None) -> Response:
    return b.client.post(
        f"/v1/apps/{b.w.app}/bundles/{bundle_id}/complete",
        headers=auth(token or b.t.builder, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def uploaded(b: Bench, data: bytes) -> str:
    """Create and upload ``data``; the bundle id."""
    r = create(b, data)
    assert r.status_code == 201, r.text
    assert upload(b, r.json()["upload"], data).status_code == 201
    return str(r.json()["bundle_id"])


def rows_of(dsn: str, org: str, sql: str, *args: object) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        return conn.execute(sql, args).fetchall()


def bundles_of(b: Bench) -> list[dict[str, Any]]:
    return rows_of(b.dsn, b.w.org, "select * from ssc.bundle order by created_at, id")


def stored_audits(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select actor_kind, actor_id, target_kind, target_id, after from ssc.audit_event "
        "where action = %s order by seq",
        AuditAction.BUNDLE_STORED.value,
    )


def with_query(url: str, **changes: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query)) | changes
    return parts._replace(query=urlencode(query)).geturl()


# ── the happy path ───────────────────────────────────────────────────────────


def test_upload_then_complete_stores_the_bundle_with_the_manifest_read_from_it(b: Bench) -> None:
    toml = b'schema = "ssc/v1"\n\n[runtime]\nport = 3000\n'
    data = tar_gz({"ssc.toml": toml, "server.js": b"listen(3000)\n"})
    commit = "c0ffee" * 6 + "abcd"
    r = create(b, data, commit=commit)
    assert r.status_code == 201, r.text
    out = r.json()
    assert r.headers["location"] == f"/v1/apps/{b.w.app}/bundles/{out['bundle_id']}"
    assert out["bundle_id"].startswith("bdl_")
    assert (out["state"], out["digest"], out["size_bytes"]) == ("pending", sha(data), len(data))
    assert (out["source_commit"], out["manifest_digest"], out["file_count"]) == (commit, None, None)
    target = out["upload"]
    assert target["method"] == "PUT"
    assert target["headers"] == {"content-length": str(len(data))}
    assert target["url"].startswith("http://testserver/blobs/bundles/")

    put = upload(b, target, data)
    assert put.status_code == 201, put.text
    assert put.headers["etag"] == f'"{sha(data).removeprefix("sha256:")}"'

    done = complete(b, out["bundle_id"])
    assert done.status_code == 200, done.text
    stored = done.json()
    want = load_manifest(toml)
    assert (stored["state"], stored["file_count"], stored["upload"]) == ("stored", 2, None)
    assert stored["manifest_digest"] == manifest_digest(want)
    assert stored["stored_at"] is not None

    got = b.client.get(f"/v1/apps/{b.w.app}/bundles/{out['bundle_id']}", headers=auth(b.t.admin))
    assert got.status_code == 200 and got.json() == stored

    [row] = bundles_of(b)
    assert Manifest.model_validate(row["manifest"]) == want
    assert (row["actor_kind"], row["actor_id"]) == ("user", b.w.builder)
    [audit] = stored_audits(b)
    assert (audit["actor_kind"], audit["actor_id"]) == ("user", b.w.builder)
    assert (audit["target_kind"], audit["target_id"]) == ("bundle", out["bundle_id"])
    assert audit["after"] == {
        "app_id": b.w.app,
        "digest": sha(data),
        "size_bytes": len(data),
        "file_count": 2,
        "manifest_digest": manifest_digest(want),
        "source_commit": commit,
    }


def test_a_bundle_without_ssc_toml_gets_the_default_manifest(b: Bench) -> None:
    data = tar_gz({"index.html": b"<p>hi</p>"})
    done = complete(b, uploaded(b, data))
    assert done.status_code == 200, done.text
    assert done.json()["manifest_digest"] == manifest_digest(load_manifest(MANIFEST))


def test_the_same_digest_is_one_bundle_and_one_object(b: Bench) -> None:
    data = tar_gz({"ssc.toml": MANIFEST, "app.py": b"print(1)\n"})
    bundle_id = uploaded(b, data)
    assert complete(b, bundle_id).status_code == 200
    again = create(b, data, b.t.admin)
    assert again.status_code == 200, again.text
    assert (again.json()["bundle_id"], again.json()["upload"]) == (bundle_id, None)
    assert again.json()["state"] == "stored"
    assert len(bundles_of(b)) == 1

    async def keys() -> list[str]:
        return [i.key async for i in b.store.list("bundles/")]

    assert asyncio.run(keys()) == [bundle_key(b.w.org, b.w.app, sha(data))]


def test_a_pending_digest_gets_a_fresh_url_and_a_different_size_is_refused(b: Bench) -> None:
    data = tar_gz({"app.py": b"print(1)\n"})
    first = create(b, data)
    b.clock.at += timedelta(minutes=11)
    second = create(b, data)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["bundle_id"] == second.json()["bundle_id"]
    assert second.json()["upload"]["expires_at"] != first.json()["upload"]["expires_at"]
    assert upload(b, first.json()["upload"], data).status_code == 403
    assert upload(b, second.json()["upload"], data).status_code == 201
    assert_problem(create(b, data, size=len(data) + 1), ErrorCode.BUNDLE_DIGEST_MISMATCH)
    assert len(bundles_of(b)) == 1


def collect(b: Bench, now: datetime) -> Collected:
    async def run() -> Collected:
        engine = make_engine(b.dsn)
        try:
            return await collect_org(engine, b.store, b.w.org, now=now)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_a_collected_upload_is_asked_for_again(b: Bench) -> None:
    data = tar_gz({"app.py": b"print(2)\n"})
    bundle_id = uploaded(b, data)
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute("update ssc.bundle set created_at = %s where id = %s", (b.clock.at, bundle_id))
    # A day on nothing has completed it: the collector deletes the object and keeps the row.
    later = b.clock.at + GRACE + timedelta(minutes=1)
    assert collect(b, later) == Collected(deleted=1)
    assert_problem(complete(b, bundle_id), ErrorCode.BUNDLE_NOT_UPLOADED)
    again = create(b, data)
    assert (again.status_code, again.json()["bundle_id"]) == (200, bundle_id)
    assert upload(b, again.json()["upload"], data).status_code == 201
    assert complete(b, bundle_id).json()["state"] == "stored"
    assert collect(b, later + GRACE) == Collected(kept=1)


def test_recording_and_completing_wait_for_the_collector(b: Bench) -> None:
    data = tar_gz({"app.py": b"print(3)\n"})
    key = bundle_key(b.w.org, b.w.app, sha(data))
    with ThreadPoolExecutor(1) as pool:
        with psycopg.connect(b.dsn) as collector:
            bind_org_sync(collector, b.w.org)
            collector.execute("select pg_advisory_xact_lock(%s, hashtext(%s))", (LOCK_CLASS, key))
            created = pool.submit(create, b, data)
            wait_for_a_lock_wait(b.dsn)
            collector.commit()
        r = created.result(timeout=10)
        assert r.status_code == 201, r.text
        bundle_id = r.json()["bundle_id"]
        assert upload(b, r.json()["upload"], data).status_code == 201
        with psycopg.connect(b.dsn) as collector:
            bind_org_sync(collector, b.w.org)
            collector.execute("select 1 from ssc.bundle where id = %s for update", (bundle_id,))
            completed = pool.submit(complete, b, bundle_id)
            wait_for_a_lock_wait(b.dsn)
            assert asyncio.run(b.store.delete(key)) is True
            collector.commit()
        # complete read the object only after the collector let go of the row.
        assert_problem(completed.result(timeout=10), ErrorCode.BUNDLE_NOT_UPLOADED)
        assert bundles_of(b)[0]["state"] == "pending"


def test_complete_twice_answers_the_stored_bundle_with_one_audit(b: Bench) -> None:
    bundle_id = uploaded(b, tar_gz({"app.py": b"x\n"}))
    first, second = complete(b, bundle_id), complete(b, bundle_id, b.t.admin)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()
    assert len(stored_audits(b)) == 1


# ── signed URLs ──────────────────────────────────────────────────────────────


def test_upload_urls_expire_after_ten_minutes_and_refuse_tampering(b: Bench) -> None:
    data = tar_gz({"app.py": b"x\n"})
    target = create(b, data).json()["upload"]
    expires = datetime.fromisoformat(target["expires_at"])
    assert expires == b.clock.now() + timedelta(minutes=10)
    for bad in (
        with_query(target["url"], sig="A" * 43),
        with_query(target["url"], len=str(len(data) + 1)),
        with_query(target["url"], exp=str(int(expires.timestamp()) + 3600)),
        target["url"] + "&sig=again",
    ):
        r = b.client.put(bad, content=data)
        assert_problem(r, ErrorCode.UPLOAD_URL_INVALID)
    b.clock.at = expires + timedelta(seconds=1)
    assert_problem(upload(b, target, data), ErrorCode.UPLOAD_URL_INVALID)
    assert asyncio.run(b.store.stat(bundle_key(b.w.org, b.w.app, sha(data)))) is None


def test_the_upload_must_be_exactly_the_signed_bytes(b: Bench) -> None:
    data = tar_gz({"app.py": b"x\n"})
    target = create(b, data).json()["upload"]
    assert_problem(b.client.put(target["url"], content=data + b"extra"), ErrorCode.BUNDLE_TOO_LARGE)
    assert_problem(b.client.put(target["url"], content=data[:-1]), ErrorCode.BUNDLE_DIGEST_MISMATCH)
    other = bytes(len(data))
    assert_problem(b.client.put(target["url"], content=other), ErrorCode.BUNDLE_DIGEST_MISMATCH)
    assert asyncio.run(b.store.stat(bundle_key(b.w.org, b.w.app, sha(data)))) is None


def test_an_object_that_is_not_the_declared_bytes_is_discarded_at_complete(b: Bench) -> None:
    data = tar_gz({"app.py": b"x\n"})
    r = create(b, data)
    key = bundle_key(b.w.org, b.w.app, sha(data))
    asyncio.run(b.store.put(key, bytes(len(data))))
    assert_problem(complete(b, r.json()["bundle_id"]), ErrorCode.BUNDLE_DIGEST_MISMATCH)
    assert asyncio.run(b.store.stat(key)) is None
    assert bundles_of(b)[0]["state"] == "pending"
    again = create(b, data)
    assert again.status_code == 200
    assert upload(b, again.json()["upload"], data).status_code == 201
    assert complete(b, again.json()["bundle_id"]).json()["state"] == "stored"


def test_a_signed_get_serves_an_attachment_and_nothing_else(b: Bench) -> None:
    data = tar_gz({"app.py": b"x\n"})
    uploaded(b, data)
    key = bundle_key(b.w.org, b.w.app, sha(data))
    url = asyncio.run(b.store.signed_url(key, method="GET")).url
    r = b.client.get(url)
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == "attachment"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert_problem(b.client.put(url, content=data), ErrorCode.UPLOAD_URL_INVALID)
    assert_problem(b.client.get(url.split("?")[0]), ErrorCode.UPLOAD_URL_INVALID)
    assert not [p for p in b.client.get("/openapi.json").json()["paths"] if "blobs" in p]


def test_the_blobs_route_is_absent_without_a_filesystem_store(
    dsns: Dsns, signing_key: SigningKey
) -> None:
    with TestClient(create_app(settings_for(dsns, signing_key))) as client:
        assert_problem(client.put("/blobs/bundles/x", content=b"x"), ErrorCode.NOT_FOUND)


def test_the_filesystem_store_is_refused_outside_dev_and_test(
    dsns: Dsns, signing_key: SigningKey, tmp_path: Path
) -> None:
    fs = settings_for(
        dsns,
        signing_key,
        blob_backend="fs",
        blob_root=str(tmp_path),
        blob_signing_keys=SIGNING_KEYS,
        blob_signing_kid="k1",
    )
    store = blob_store_for(replace(fs, public_url="http://localhost:9000/"))
    assert isinstance(store, FsBlobStore)
    url = asyncio.run(store.signed_url("bundles/x", method="GET")).url
    assert url.startswith("http://localhost:9000/blobs/bundles/x?")
    for env in ("prod", "staging", ""):
        with pytest.raises(ValueError, match="filesystem blob store"):
            create_app(replace(fs, environment=env))
    with pytest.raises(ValueError, match="SSC_BLOB_ROOT"):
        blob_store_for(replace(fs, blob_root=""))
    with pytest.raises(ValueError, match="unknown SSC_BLOB_BACKEND"):
        blob_store_for(replace(fs, blob_backend="s3"))
    assert blob_store_for(replace(fs, blob_backend="none")) is None


def test_settings_read_the_blob_and_bundle_environment() -> None:
    key = base64.b64encode(b"s" * 32).decode()
    env = {
        "SSC_DATABASE_DSN": "postgresql://ssc_app@localhost/ssc",
        "SSC_API_JWKS": '{"keys": []}',
        "SSC_API_ISSUER": ISSUER,
        "SSC_ENV": "dev",
        "SSC_BLOB_BACKEND": "fs",
        "SSC_BLOB_ROOT": "/var/ssc/blobs",
        "SSC_BLOB_SIGNING_KEYS": json.dumps({"k2": key}),
        "SSC_BLOB_SIGNING_KID": "k2",
        "SSC_BUNDLE_MAX_BYTES": "10",
        "SSC_BUNDLE_MAX_UNPACKED_BYTES": "20",
        "SSC_BUNDLE_MAX_FILES": "3",
    }
    s = Settings.from_env(env)
    assert (s.environment, s.blob_backend, s.blob_root) == ("dev", "fs", "/var/ssc/blobs")
    assert (s.blob_signing_keys, s.blob_signing_kid) == ({"k2": b"s" * 32}, "k2")
    assert (s.bundle_max_bytes, s.bundle_max_unpacked_bytes, s.bundle_max_files) == (10, 20, 3)
    assert key not in repr(s)
    defaults = Settings.from_env({k: env[k] for k in list(env)[:3]})
    assert (defaults.environment, defaults.blob_backend) == ("prod", "none")
    # Each org's bundles go to its cell's bucket, signed as SSC_BLOB_SIGNER (decision 015).
    assert defaults.cell_bucket_template == ""
    assert cell_stores_for(defaults) is None
    signer = "ssc-control@ssc-control-prod.iam.gserviceaccount.com"
    cells = Settings.from_env(
        env | {"SSC_CELL_BUCKET_TEMPLATE": "ssc-c-{cell}-cell", "SSC_BLOB_SIGNER": signer}
    )
    assert cells.cell_bucket_template == "ssc-c-{cell}-cell"
    assert cell_stores_for(cells) is not None
    with pytest.raises(StorageConfigError):
        cell_stores_for(replace(cells, cell_bucket_template="ssc-c-cell"))
    for bad in ("[]", '{"k": "not base64!"}'):
        with pytest.raises(ValueError):
            Settings.from_env(env | {"SSC_BLOB_SIGNING_KEYS": bad})


@pytest.mark.parametrize(
    "overrides",
    [
        {"SSC_BLOB_BACKEND": "s3"},
        {"SSC_BLOB_ROOT": ""},
        {"SSC_ENV": "prod"},
        {"SSC_BLOB_SIGNING_KID": "k9"},
        {"SSC_BLOB_SIGNING_KEYS": json.dumps({"k1": base64.b64encode(b"short").decode()})},
        {"SSC_BLOB_SIGNING_KEYS": "k1"},
    ],
    ids=["backend", "root", "environment", "kid", "weak-key", "not-json"],
)
def test_the_api_and_the_worker_share_one_store_factory(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    env = {
        "SSC_DATABASE_DSN": "postgresql://ssc_app@localhost/ssc",
        "SSC_API_JWKS": '{"keys": []}',
        "SSC_API_ISSUER": ISSUER,
        "SSC_ENV": "test",
        "SSC_BLOB_BACKEND": "fs",
        "SSC_BLOB_ROOT": str(tmp_path),
        "SSC_BLOB_SIGNING_KEYS": json.dumps({"k1": base64.b64encode(b"k" * 32).decode()}),
        "SSC_BLOB_SIGNING_KID": "k1",
    }
    assert isinstance(blob_store_for(Settings.from_env(env)), FsBlobStore)
    with pytest.raises(StorageConfigError) as worker:
        blob_store_from_env(env | overrides)
    with pytest.raises(StorageConfigError) as api:
        blob_store_for(Settings.from_env(env | overrides))
    assert str(api.value) == str(worker.value)


def test_the_gcs_store_is_one_bucket_signed_as_one_service_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[str, str]] = []

    def fake_gcs(bucket: str, signer: str) -> FsBlobStore:
        built.append((bucket, signer))
        return cast(FsBlobStore, object())

    monkeypatch.setattr(storage, "gcs_store", fake_gcs)
    signer = "ssc-control@ssc-control-staging.iam.gserviceaccount.com"
    env = {
        "SSC_DATABASE_DSN": "postgresql://ssc_app@localhost/ssc",
        "SSC_API_JWKS": '{"keys": []}',
        "SSC_API_ISSUER": ISSUER,
        "SSC_BLOB_BACKEND": "gcs",
        "SSC_BLOB_BUCKET": "ssc-c-testcell01-cell",
        "SSC_BLOB_SIGNER": signer,
    }
    s = Settings.from_env(env)
    assert (s.environment, s.blob_bucket, s.blob_signer) == ("prod", env["SSC_BLOB_BUCKET"], signer)
    blob_store_for(s)
    blob_store_from_env(env)
    assert built == [(env["SSC_BLOB_BUCKET"], signer)] * 2
    for bad in (
        {"SSC_BLOB_BUCKET": ""},
        {"SSC_BLOB_BUCKET": "Bad_Bucket"},
        {"SSC_BLOB_SIGNER": ""},
        {"SSC_BLOB_SIGNER": "someone@example.com"},
    ):
        with pytest.raises(StorageConfigError) as worker:
            blob_store_from_env(env | bad)
        with pytest.raises(StorageConfigError) as api:
            blob_store_for(Settings.from_env(env | bad))
        assert str(api.value) == str(worker.value)
    assert len(built) == 2


# ── the server's checks ──────────────────────────────────────────────────────


def test_complete_before_upload_is_not_uploaded(b: Bench) -> None:
    r = create(b, tar_gz({"app.py": b"x\n"}))
    assert_problem(complete(b, r.json()["bundle_id"]), ErrorCode.BUNDLE_NOT_UPLOADED)
    assert bundles_of(b)[0]["state"] == "pending"


def test_an_env_file_is_malformed(b: Bench) -> None:
    bundle_id = uploaded(b, tar_gz({"app.py": b"x\n", "config/.env": b"TOKEN=1\n"}))
    assert_problem(complete(b, bundle_id), ErrorCode.BUNDLE_MALFORMED)
    assert bundles_of(b)[0]["state"] == "pending"
    assert stored_audits(b) == []


def test_a_service_role_key_is_a_secret_in_bundle(
    b: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    key = supabase_jwt(SERVICE_ROLE_ROLE)
    src = f'createClient(url, "{key}")\n'.encode()
    bundle_id = uploaded(b, tar_gz({"ssc.toml": MANIFEST, "src/db.js": src}))
    with caplog.at_level(logging.WARNING):
        assert_problem(complete(b, bundle_id), ErrorCode.SECRET_IN_BUNDLE)
    assert "src/db.js" in caplog.text
    assert key not in caplog.text and key[-12:] not in caplog.text
    assert bundles_of(b)[0]["state"] == "pending"


def test_a_value_declared_public_is_not_a_secret(b: Bench) -> None:
    anon = supabase_jwt("anon")
    toml = f'schema = "ssc/v1"\n\n[build.public_env.prod]\nVITE_KEY = "{anon}"\n'.encode()
    bundle_id = uploaded(b, tar_gz({"ssc.toml": toml, "src/db.js": anon.encode()}))
    assert complete(b, bundle_id).status_code == 200


def test_an_invalid_manifest_is_refused(b: Bench) -> None:
    bundle_id = uploaded(b, tar_gz({"ssc.toml": b'schema = "ssc/v9"\n'}))
    assert_problem(complete(b, bundle_id), ErrorCode.MANIFEST_INVALID)


def test_a_bundle_over_the_size_cap_is_refused_before_any_url(make_bench: Any) -> None:
    b = make_bench(bundle_max_bytes=100)
    assert_problem(create(b, b"x" * 101), ErrorCode.BUNDLE_TOO_LARGE)
    assert bundles_of(b) == []


def test_bundles_over_the_unpacked_or_file_caps_are_refused_at_complete(make_bench: Any) -> None:
    b = make_bench(bundle_max_unpacked_bytes=1000, bundle_max_files=3)
    bomb = uploaded(b, tar_gz({"zeros.bin": bytes(1001)}))
    assert_problem(complete(b, bomb), ErrorCode.BUNDLE_TOO_LARGE)
    many = uploaded(b, tar_gz({f"f{i}.txt": b"x" for i in range(4)}))
    assert_problem(complete(b, many), ErrorCode.BUNDLE_TOO_LARGE)
    assert {r["state"] for r in bundles_of(b)} == {"pending"}


# ── who may ship ─────────────────────────────────────────────────────────────


def test_only_admins_owners_and_builders_may_ship(b: Bench) -> None:
    data = tar_gz({"app.py": b"x\n"})
    assert_problem(create(b, data, b.t.member), ErrorCode.FORBIDDEN)
    bundle_id = uploaded(b, data)
    assert_problem(complete(b, bundle_id, b.t.member), ErrorCode.FORBIDDEN)
    get = b.client.get(f"/v1/apps/{b.w.app}/bundles/{bundle_id}", headers=auth(b.t.member))
    assert_problem(get, ErrorCode.FORBIDDEN)
    assert complete(b, bundle_id, b.t.admin).status_code == 200


def test_unknown_apps_and_bundles_are_not_found(b: Bench) -> None:
    data = tar_gz({"app.py": b"x\n"})
    assert_problem(create(b, data, app=new_id("app")), ErrorCode.NOT_FOUND)
    assert_problem(complete(b, new_id("bdl")), ErrorCode.NOT_FOUND)
    get = b.client.get(f"/v1/apps/{b.w.app}/bundles/{new_id('bdl')}", headers=auth(b.t.builder))
    assert_problem(get, ErrorCode.NOT_FOUND)


def test_a_disabled_app_takes_no_new_source(b: Bench) -> None:
    data = tar_gz({"app.py": b"x\n"})
    bundle_id = uploaded(b, data)
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute("update ssc.app set status = 'disabled' where id = %s", (b.w.app,))
    assert_problem(create(b, tar_gz({"other.py": b"y\n"})), ErrorCode.APP_NOT_ACTIVE)
    assert_problem(complete(b, bundle_id), ErrorCode.APP_NOT_ACTIVE)


# ── the manifest a release reads ─────────────────────────────────────────────


def add_release(b: Bench, number: int, source_digest: str, manifest_digest_: str) -> str:
    rid = new_id("rel")
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute(
            "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
            "source_digest, actor_kind, actor_id) values (%s, %s, %s, %s, %s, %s, %s, 'user', %s)",
            (
                rid,
                b.w.org,
                b.w.app,
                number,
                sha(b"image"),
                manifest_digest_,
                source_digest,
                b.w.admin,
            ),
        )
    return rid


def test_a_release_reads_the_manifest_stored_with_its_bundle(b: Bench) -> None:
    toml = b'schema = "ssc/v1"\n\n[runtime]\nport = 8080\n'
    data = tar_gz({"ssc.toml": toml})
    want = load_manifest(toml)
    pending = tar_gz({"other.txt": b"y"})
    assert complete(b, uploaded(b, data)).status_code == 200
    assert create(b, pending).status_code == 201
    good = add_release(b, 1, sha(data), manifest_digest(want))
    other_digest = add_release(b, 2, sha(data), sha(b"not it"))
    no_bundle = add_release(b, 3, sha(b"never uploaded"), manifest_digest(want))
    still_pending = add_release(b, 4, sha(pending), manifest_digest(want))

    async def read() -> tuple[list[Manifest | None], Manifest]:
        engine = make_engine(b.dsn)
        try:
            async with bound_org(engine, b.w.org) as conn:
                found = [
                    await release_manifest(conn, b.w.org, rid)
                    for rid in (good, other_digest, no_bundle, still_pending)
                ]
                spec = await BundleReleaseSpecs().get(
                    conn, org_id=b.w.org, app_id=b.w.app, release_id=good
                )
                with pytest.raises(ReleaseSpecUnavailableError):
                    await BundleReleaseSpecs().get(
                        conn, org_id=b.w.org, app_id=b.w.app, release_id=no_bundle
                    )
                return found, spec.manifest
        finally:
            await engine.dispose()

    found, spec_manifest = asyncio.run(read())
    assert found == [want, None, None, None]
    assert spec_manifest == want
