"""The file broker end to end over ASGI (SSC-046). The bucket is a local stand-in for Cloud
Storage that checks each V4 signature against the signer's public key, its expiry, its signed
headers and its length range, as the bucket does; ``v4_url`` itself is checked against Google's
client library in ``ssc_shared``'s ``test_blobstore_gcs``.

Done when: a test app uploads a photo and downloads it; a link for app A is refused for app B's
prefix; a disabled app cannot obtain links; a stored HTML file downloads instead of rendering."""

import hashlib
import json
import logging
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx2
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from datagw_world import (
    AUDIENCE,
    CERTS,
    ENV,
    FORGED_KEY,
    ORG,
    PAY,
    PREVIEW,
    PROD,
    PROJECT,
    SETTINGS,
    FakeConnector,
    bearer,
    certs_transport,
    publish,
    store,
)
from fastapi import FastAPI
from google.api_core.exceptions import NotFound, ServiceUnavailable
from google.auth import crypt

from ssc_datagw.admission import RECHECK_SECONDS, OnDemandSnapshot
from ssc_datagw.files import LINK_SECONDS, MAX_FILE_BYTES, FileBroker
from ssc_datagw.server import DataGateway, create_app, production_app
from ssc_datagw.workload import GoogleWorkloads
from ssc_shared.access import ViewHolder
from ssc_shared.blobstore_fs import FsBlobStore
from ssc_shared.blobstore_gcs import KeySigner
from ssc_shared.snapshot_feed import SnapshotFeed, latest_key

BUCKET = SETTINGS.bucket
STORAGE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
HTML = b"<!doctype html><script>alert(document.cookie)</script>"
KILLS = {"a disabled app": "disabled", "a quarantined app": "quarantined"}


class Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Stored:
    body: bytes
    content_type: str

    @property
    def size(self) -> int:
        return len(self.body)


class Storage:
    """The cell bucket, as the broker lists it and as an app reaches it with a signed link."""

    name = BUCKET

    def __init__(self) -> None:
        self.objects: dict[str, Stored] = {}
        self.now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
        self.down = False
        pem = STORAGE_KEY.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.signer = KeySigner(SETTINGS.signer, crypt.RSASigner.from_string(pem))

    def _up(self) -> None:
        if self.down:
            raise ServiceUnavailable("storage is down")

    def get_blob(self, blob_name: str, *, timeout: float) -> Stored | None:
        self._up()
        return self.objects.get(blob_name)

    def list_blobs(self, *, prefix: str, timeout: float) -> Iterator[Stored]:
        self._up()
        return iter([o for k, o in self.objects.items() if k.startswith(prefix)])

    def delete_blob(self, blob_name: str, *, timeout: float) -> None:
        self._up()
        if self.objects.pop(blob_name, None) is None:
            raise NotFound(blob_name)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        raw_path, _, raw_query = request.url.raw_path.decode().partition("?")
        params = dict(parse_qsl(raw_query, keep_blank_values=True))
        signature = params.pop("X-Goog-Signature", "")
        names = params.get("X-Goog-SignedHeaders", "").split(";")
        values = {n: request.url.host if n == "host" else request.headers.get(n, "") for n in names}
        canonical = "\n".join(
            [
                request.method,
                raw_path,
                "&".join(
                    f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params.items())
                ),
                "".join(f"{n}:{values[n]}\n" for n in names),
                ";".join(names),
                "UNSIGNED-PAYLOAD",
            ]
        )
        date = params.get("X-Goog-Date", "")
        scope = f"{date[:8]}/auto/storage/goog4_request"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        to_sign = "\n".join(["GOOG4-RSA-SHA256", date, scope, digest]).encode()
        try:
            STORAGE_KEY.public_key().verify(
                bytes.fromhex(signature), to_sign, padding.PKCS1v15(), hashes.SHA256()
            )
        except InvalidSignature, ValueError:
            return httpx2.Response(403, text="SignatureDoesNotMatch")
        issued = datetime.strptime(date, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if self.now > issued + timedelta(seconds=int(params["X-Goog-Expires"])):
            return httpx2.Response(400, text="ExpiredToken")
        bucket, _, key = unquote(raw_path).removeprefix("/").partition("/")
        assert bucket == BUCKET
        if request.method == "PUT":
            low, high = map(int, values.get("x-goog-content-length-range", "0,0").split(","))
            if not low <= len(request.content) <= high:
                return httpx2.Response(400, text="EntityTooLarge")
            self.objects[key] = Stored(request.content, request.headers["content-type"])
            return httpx2.Response(200)
        stored = self.objects.get(key)
        if stored is None:
            return httpx2.Response(404, text="NoSuchKey")
        headers = {"content-type": stored.content_type}
        if "response-content-disposition" in params:
            headers["content-disposition"] = params["response-content-disposition"]
        return httpx2.Response(200, content=stored.body, headers=headers)


def workloads() -> GoogleWorkloads:
    return GoogleWorkloads(
        audience=AUDIENCE, project_id=PROJECT, transport=certs_transport(), certs_url=CERTS
    )


def client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://datagw")


@dataclass
class World:
    blobs: FsBlobStore
    storage: Storage
    clock: Clock
    http: httpx2.AsyncClient
    bucket: httpx2.AsyncClient

    async def ask(
        self,
        op: str,
        name: str = "photos/cat.png",
        *,
        env_id: str = PROD,
        headers: Mapping[str, str] | None = None,
        **body: Any,
    ) -> httpx2.Response:
        sent = {**bearer(env_id), **(headers or {})}
        return await self.http.post(f"/v1/files/{op}", json={"name": name, **body}, headers=sent)

    async def link(self, op: str, name: str = "photos/cat.png", **kw: Any) -> dict[str, Any]:
        r = await self.ask(op, name, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    async def send(
        self, link: Mapping[str, Any], body: bytes = b"", *, url: str | None = None
    ) -> httpx2.Response:
        return await self.bucket.request(
            link["method"],
            url or link["url"],
            headers=link["headers"],
            content=body if link["method"] == "PUT" else None,
        )


@asynccontextmanager
async def running(
    tmp_path: Path, *, quota: int | None = None, broker: bool = True, **changes: Any
) -> AsyncGenerator[World]:
    blobs = store(tmp_path)
    await publish(blobs, 1, **changes)
    clock = Clock()
    holder = ViewHolder(ORG)
    feed = SnapshotFeed(blobs, holder, monotonic=clock)
    snapshot = OnDemandSnapshot(feed, holder, max_stale=SETTINGS.max_stale)
    storage = Storage()
    files = FileBroker(
        storage,
        storage.signer,
        clock=lambda: storage.now,
        **({} if quota is None else {"quota": quota}),
    )
    gateway = DataGateway(
        settings=SETTINGS,
        workloads=workloads(),
        snapshot=snapshot,
        connectors={},
        files=files if broker else None,
    )
    async with (
        client(create_app(gateway)) as http,
        httpx2.AsyncClient(transport=httpx2.MockTransport(storage.handle)) as bucket,
    ):
        try:
            yield World(blobs, storage, clock, http, bucket)
        finally:
            await gateway.aclose()
            await snapshot.aclose()


def error(response: httpx2.Response) -> dict[str, Any]:
    body = response.json()
    assert body["request_id"] == response.headers["x-request-id"]
    return body["error"]


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    prefix = "datagw file "
    return [
        json.loads(r.getMessage().removeprefix(prefix))
        for r in caplog.records
        if r.getMessage().startswith(prefix)
    ]


async def test_done_when_a_test_app_uploads_a_photo_and_downloads_it(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        put = await w.link("put", content_type="image/png")
        stored = await w.send(put, PNG)
        get = await w.link("get")
        got = await w.send(get)
    expires = (w.storage.now + timedelta(seconds=LINK_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert {k: put[k] for k in ("method", "headers", "expires_at", "max_bytes")} == {
        "method": "PUT",
        "headers": {
            "content-type": "image/png",
            "x-goog-content-length-range": f"0,{MAX_FILE_BYTES}",
        },
        "expires_at": expires,
        "max_bytes": 25 * 1024 * 1024,
    }
    assert urlsplit(put["url"]).netloc == "storage.googleapis.com"
    assert urlsplit(put["url"]).path == f"/{BUCKET}/files/{PROD}/photos/cat.png"
    assert (get["method"], get["headers"], get["expires_at"]) == ("GET", {}, expires)
    assert "max_bytes" not in get
    assert stored.status_code == 200
    assert (got.status_code, got.content) == (200, PNG)
    assert got.headers["content-type"] == "image/png"
    assert got.headers["content-disposition"] == 'attachment; filename="cat.png"'
    assert list(w.storage.objects) == [f"files/{PROD}/photos/cat.png"]


async def test_done_when_a_link_for_app_a_is_refused_for_app_b_prefix(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        put = await w.link("put", content_type="image/png")
        assert (await w.send(put, PNG)).status_code == 200
        get = await w.link("get")
        theirs = {
            op: link["url"].replace(f"/files/{PROD}/", f"/files/{PAY}/")
            for op, link in (("put", put), ("get", get))
        }
        w.storage.objects[f"files/{PAY}/photos/cat.png"] = w.storage.objects[
            f"files/{PROD}/photos/cat.png"
        ]
        put_there = await w.send(put, b"overwritten", url=theirs["put"])
        get_there = await w.send(get, url=theirs["get"])
        b_asks_for_a = await w.ask("get", env_id=PAY, name="photos/elsewhere.png")
        preview_asks = await w.ask("get", env_id=PREVIEW)
    assert (put_there.status_code, get_there.status_code) == (403, 403)
    assert w.storage.objects[f"files/{PAY}/photos/cat.png"].body == PNG
    assert (b_asks_for_a.status_code, error(b_asks_for_a)["code"]) == (404, "FILE_NOT_FOUND")
    assert (preview_asks.status_code, error(preview_asks)["code"]) == (404, "FILE_NOT_FOUND")


NAMES = {
    "a parent segment": "../env_yyyyyyyyyyyyyyyyyyyy/photos/cat.png",
    "a parent segment inside": "photos/../../cat.png",
    "a leading slash": "/files/env_yyyyyyyyyyyyyyyyyyyy/cat.png",
    "an empty segment": "photos//cat.png",
    "a hidden segment": "photos/.cat.png",
    "a trailing slash": "photos/",
    "a space": "my cat.png",
    "a quote": 'cat".png',
    "a backslash": "photos\\cat.png",
    "a percent escape": "photos%2F..%2Fcat.png",
    "too long": "a" * 257,
    "empty": "",
}


@pytest.mark.parametrize("op", ["put", "get", "delete"])
@pytest.mark.parametrize("case", sorted(NAMES))
async def test_a_name_that_could_leave_the_environment_is_refused(
    tmp_path: Path, case: str, op: str
) -> None:
    async with running(tmp_path) as w:
        r = await w.ask(op, NAMES[case])
    assert (r.status_code, error(r)["code"]) == (422, "VALIDATION_FAILED")
    assert w.storage.objects == {}


@pytest.mark.parametrize("op", ["put", "get", "delete"])
@pytest.mark.parametrize("case", sorted(KILLS))
async def test_done_when_a_disabled_app_cannot_obtain_links(
    tmp_path: Path, case: str, op: str
) -> None:
    async with running(tmp_path, status=KILLS[case]) as w:
        r = await w.ask(op, content_type="image/png" if op == "put" else None)
    assert (r.status_code, error(r)["code"], error(r)["fix_owner"]) == (
        403,
        "APP_NOT_ACTIVE",
        "admin",
    )


async def test_the_kill_switch_suspends_the_broker_for_a_running_gateway(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        assert (await w.ask("put")).status_code == 200
        await publish(w.blobs, 2, status="disabled")
        w.clock.now += RECHECK_SECONDS + 1
        r = await w.ask("put")
        other_app = await w.ask("put", env_id=PAY)
    assert (r.status_code, error(r)["code"]) == (403, "APP_NOT_ACTIVE")
    assert other_app.status_code == 200


async def test_done_when_a_stored_html_file_downloads_instead_of_rendering(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as w:
        put = await w.link("put", "reports/page.html", content_type="text/html; charset=utf-8")
        assert (await w.send(put, HTML)).status_code == 200
        get = await w.link("get", "reports/page.html")
        got = await w.send(get)
        inline = get["url"].replace("attachment", "inline")
        made_inline = await w.send(get, url=inline)
        dropped = await w.send(
            get,
            url="&".join(
                p for p in get["url"].split("&") if not p.startswith("response-content-disposition")
            ),
        )
    assert (got.status_code, got.content) == (200, HTML)
    assert got.headers["content-disposition"] == 'attachment; filename="page.html"'
    assert (made_inline.status_code, dropped.status_code) == (403, 403)


async def test_the_bucket_refuses_a_body_over_25_mb_or_of_another_type(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        put = await w.link("put", content_type="image/png")
        too_big = await w.send(put, b"\0" * (MAX_FILE_BYTES + 1))
        just_fits = await w.send(put, b"\0" * MAX_FILE_BYTES)
        retyped = await w.send({**put, "headers": {**put["headers"], "content-type": "text/html"}})
        unbounded = await w.send({**put, "headers": {"content-type": "image/png"}}, PNG)
    assert (too_big.status_code, just_fits.status_code) == (400, 200)
    assert (retyped.status_code, unbounded.status_code) == (403, 403)


async def test_a_link_lives_ten_minutes(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        put = await w.link("put")
        w.storage.now += timedelta(seconds=LINK_SECONDS)
        in_time = await w.send(put, PNG)
        get = await w.link("get")
        w.storage.now += timedelta(seconds=LINK_SECONDS + 1)
        late = await w.send(get)
    assert (in_time.status_code, late.status_code) == (200, 400)


async def test_the_quota_is_per_environment_and_delete_frees_it(tmp_path: Path) -> None:
    async with running(tmp_path, quota=len(PNG)) as w:
        put = await w.link("put")
        assert (await w.send(put, PNG)).status_code == 200
        full = await w.ask("put", "photos/dog.png")
        other_env = await w.ask("put", "photos/dog.png", env_id=PREVIEW)
        deleted = await w.ask("delete")
        again = await w.ask("delete")
        freed = await w.ask("put", "photos/dog.png")
    assert (full.status_code, error(full)["code"], error(full)["fix_owner"]) == (
        413,
        "FILES_QUOTA_EXCEEDED",
        "app",
    )
    assert (other_env.status_code, freed.status_code) == (200, 200)
    assert (deleted.status_code, deleted.json()["deleted"]) == (200, True)
    assert (again.status_code, error(again)["code"]) == (404, "FILE_NOT_FOUND")
    assert w.storage.objects == {}


async def test_a_file_that_is_not_there_is_not_found(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        r = await w.ask("get")
    assert (r.status_code, error(r)["code"], error(r)["stage"]) == (404, "FILE_NOT_FOUND", "files")


REFUSALS = {
    "an operation that does not exist": (
        lambda w: w.ask("list"),
        (404, "NOT_FOUND"),
    ),
    "a forged token": (
        lambda w: w.ask("get", headers=bearer(key=FORGED_KEY)),
        (401, "UNAUTHENTICATED"),
    ),
    "an environment not in the snapshot": (
        lambda w: w.ask("get", env_id="env_" + "z" * 20),
        (403, "UNKNOWN_ENVIRONMENT"),
    ),
    "an unknown key": (lambda w: w.ask("get", size=3), (422, "VALIDATION_FAILED")),
    "a content type that is not one": (
        lambda w: w.ask("put", content_type="not a type"),
        (422, "VALIDATION_FAILED"),
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
async def test_file_requests_are_refused_like_queries(tmp_path: Path, case: str) -> None:
    call, expect = REFUSALS[case]
    async with running(tmp_path) as w:
        r = await call(w)
    assert (r.status_code, error(r)["code"]) == expect


async def test_a_body_that_is_not_json_is_refused(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        r = await w.http.post("/v1/files/put", content=b"{", headers=bearer())
    assert (r.status_code, error(r)["code"]) == (422, "VALIDATION_FAILED")


async def test_a_stale_snapshot_gives_no_link(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        assert (await w.ask("put")).status_code == 200
        await w.blobs.delete(latest_key(ORG))
        w.clock.now += SETTINGS.max_stale + 1
        r = await w.ask("put")
    assert (r.status_code, error(r)["code"]) == (503, "DATA_SNAPSHOT_STALE")


async def test_storage_that_does_not_answer_is_unavailable(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        w.storage.down = True
        answers = [await w.ask(op) for op in ("put", "get", "delete")]
    for r in answers:
        assert (r.status_code, error(r)["code"], error(r)["fix_owner"]) == (
            503,
            "FILES_UNAVAILABLE",
            "platform",
        )


async def test_a_gateway_without_a_broker_says_files_are_unavailable(tmp_path: Path) -> None:
    async with running(tmp_path, broker=False) as w:
        r = await w.ask("put")
    assert (r.status_code, error(r)["code"]) == (503, "FILES_UNAVAILABLE")


async def test_logs_carry_the_operation_and_never_the_name_or_the_link(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with running(tmp_path) as w:
        put = await w.link("put", "payslips/ada-lovelace-2026-09.pdf")
        missing = await w.ask("get", "payslips/ben-2026-09.pdf")
    assert missing.status_code == 404
    for text in ("ada-lovelace", "ben-2026", "payslips", put["url"].rsplit("=", 1)[-1]):
        assert text not in caplog.text
    served, refused = records(caplog)
    assert served | {"request_id": "", "received_at": "", "elapsed_ms": 0} == {
        "request_id": "",
        "file_op": "put",
        "env_id": PROD,
        "snapshot_version": 1,
        "outcome": "served",
        "received_at": "",
        "elapsed_ms": 0,
        "instance_started_at": served["instance_started_at"],
        "cold": True,
        "ready_ms": None,
    }
    assert (refused["file_op"], refused["outcome"]) == ("get", "FILE_NOT_FOUND")


async def test_the_production_app_serves_the_broker_it_is_given(tmp_path: Path) -> None:
    blobs = store(tmp_path)
    await publish(blobs, 1)
    storage = Storage()
    broker = FileBroker(storage, storage.signer, clock=lambda: storage.now)
    with_files = production_app(ENV, store=blobs, workloads=workloads(), files=broker)
    without = production_app(
        ENV, store=blobs, workloads=workloads(), connectors={"x": FakeConnector()}
    )
    body = {"name": "photos/cat.png", "content_type": "image/png"}
    answers: list[httpx2.Response] = []
    for app in (with_files, without):
        async with app.router.lifespan_context(app), client(app) as http:
            answers.append(await http.post("/v1/files/put", json=body, headers=bearer()))
    served, unavailable = answers
    assert served.status_code == 200
    assert served.json()["url"].startswith(f"https://storage.googleapis.com/{BUCKET}/files/{PROD}/")
    assert (unavailable.status_code, error(unavailable)["code"]) == (503, "FILES_UNAVAILABLE")
