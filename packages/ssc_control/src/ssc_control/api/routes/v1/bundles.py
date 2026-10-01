"""Source bundles (SSC-014): ask for an upload URL, upload the bytes, then complete.

``POST /apps/{app_id}/bundles`` records the digest and answers with a signed PUT URL that lives
at most 10 minutes and is bound to the length and sha256. The same digest again answers 200: the
stored bundle with no URL, or a fresh URL while it is still pending. ``complete`` re-reads the
object and runs every check server side (``ssc_control.deploy.bundles``) before the bundle is
stored with the manifest read from it. A release built from a bundle carries
``source_digest = digest`` (decision 015). Recording a digest takes its key's advisory lock and
``complete`` locks the row, so the bundle collector (``deploy.bundle_gc``) skips or waits for
both. An object that is not the declared bytes is deleted at ``complete``, because a bucket URL
only creates: the client then asks for a fresh URL and uploads again.
"""

import json
import logging
from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy import RowMapping, text

from ssc_bundle.limits import Limits
from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.authz import require_app_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.runtime import Runtime, runtime_of
from ssc_control.api.settings import Settings
from ssc_control.api.uow import UnitOfWork, UserUoW
from ssc_control.deploy.bundle_gc import lock_bundle_key
from ssc_control.deploy.bundles import DIGEST_PREFIX, BundleRejectedError, bundle_key, check_upload
from ssc_shared.blobstore import BlobError, BlobStore

log = logging.getLogger(__name__)
router = APIRouter()

MAX_SAFE_INTEGER: Final = 2**53 - 1

Digest = Annotated[
    str,
    Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="sha256 of the `.tar.gz` bytes, as `sha256:<64 hex>`.",
    ),
]


class BundleCreate(Strict):
    digest: Digest
    size_bytes: int = Field(gt=0, le=MAX_SAFE_INTEGER, description="Length of the `.tar.gz`.")
    source_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class UploadTarget(Strict):
    method: Literal["PUT"]
    url: str = Field(description="A credential until `expires_at`: do not log or share it.")
    headers: dict[str, str] = Field(description="Send these headers with the PUT.")
    expires_at: datetime


class BundleOut(Strict):
    bundle_id: str
    app_id: str
    digest: str
    size_bytes: int
    state: Literal["pending", "stored"]
    source_commit: str | None
    manifest_digest: str | None = Field(description="Of the manifest read from the bundle.")
    file_count: int | None
    created_at: datetime
    stored_at: datetime | None
    upload: UploadTarget | None = Field(
        description="Where to PUT the bytes while pending; null once stored and on reads."
    )


_SELECT_APP_STATUS = text("select status from ssc.app where org_id = :org and id = :app")
_INSERT_BUNDLE = text(
    "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, source_commit, actor_kind, "
    "actor_id, actor_via_agent, actor_client_id) values (:id, :org, :app, :digest, :size, "
    ":commit, :actor_kind, :actor_id, :via_agent, :client_id) "
    "on conflict (org_id, app_id, digest) do nothing returning id"
)
_SELECT_BY_DIGEST = text(
    "select id as bundle_id, app_id, digest, size_bytes, state, source_commit, manifest_digest, "
    "file_count, created_at, stored_at from ssc.bundle "
    "where org_id = :org and app_id = :app and digest = :digest"
)
_SELECT_BY_ID = text(
    "select id as bundle_id, app_id, digest, size_bytes, state, source_commit, manifest_digest, "
    "file_count, created_at, stored_at from ssc.bundle "
    "where org_id = :org and app_id = :app and id = :id"
)
_LOCK_BY_ID = text(_SELECT_BY_ID.text + " for update")
_STORE = text(
    "update ssc.bundle set state = 'stored', manifest = cast(:manifest as jsonb), "
    "manifest_digest = :manifest_digest, file_count = :file_count, stored_at = now() "
    "where org_id = :org and id = :id and state = 'pending' "
    "returning id as bundle_id, app_id, digest, size_bytes, state, source_commit, "
    "manifest_digest, file_count, created_at, stored_at"
)


def limits_of(settings: Settings) -> Limits:
    return Limits(
        max_bytes=settings.bundle_max_bytes,
        max_unpacked_bytes=settings.bundle_max_unpacked_bytes,
        max_files=settings.bundle_max_files,
    )


def _store_of(rt: Runtime) -> BlobStore:
    if rt.blob_store is None:
        raise Refusal(ErrorCode.INTERNAL, evidence={"reason": "no_blob_store"})
    return rt.blob_store


async def _app_status_for_builder(uow: UnitOfWork, app_id: str) -> str:
    """``NOT_FOUND`` for an app the org does not have, then ``FORBIDDEN`` for a non-builder."""
    params = {"org": uow.org_id, "app": app_id}
    status = (await uow.conn.execute(_SELECT_APP_STATUS, params)).scalar_one_or_none()
    if status is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id})
    await require_app_builder(uow, app_id)
    return str(status)


async def _bundle(
    uow: UnitOfWork, app_id: str, bundle_id: str, *, lock: bool = False
) -> RowMapping:
    params = {"org": uow.org_id, "app": app_id, "id": bundle_id}
    sql = _LOCK_BY_ID if lock else _SELECT_BY_ID
    row = (await uow.conn.execute(sql, params)).mappings().first()
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"bundle_id": bundle_id})
    return row


async def _discard(store: BlobStore, key: str) -> None:
    """Delete an object that is not the declared bytes; the row stays ``pending``."""
    try:
        await store.delete(key)
    except BlobError:
        log.warning("bundle object not discarded", extra={"key": key})


def _out(row: RowMapping, upload: UploadTarget | None = None) -> BundleOut:
    return BundleOut(**dict(row), upload=upload)


def _location(app_id: str, bundle_id: str) -> dict[str, str]:
    return {"Location": f"/v1/apps/{app_id}/bundles/{bundle_id}"}


@router.post(
    "/apps/{app_id}/bundles",
    status_code=201,
    response_model=BundleOut,
    dependencies=[UserIdempotent],
    responses={
        200: {
            "model": BundleOut,
            "description": "This app already has the digest: stored (no upload), or still "
            "pending (a fresh upload URL).",
        },
        **problem_responses(
            *POST_COMMON,
            ErrorCode.NOT_FOUND,
            ErrorCode.FORBIDDEN,
            ErrorCode.APP_NOT_ACTIVE,
            ErrorCode.BUNDLE_TOO_LARGE,
            ErrorCode.BUNDLE_DIGEST_MISMATCH,
        ),
    },
)
async def create_bundle(app_id: Id, body: BundleCreate, request: Request, uow: UserUoW) -> Response:
    """Record a bundle by digest and answer with where to upload it."""
    rt = runtime_of(request)
    status = await _app_status_for_builder(uow, app_id)
    if status != "active":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"status": status})
    if body.size_bytes > rt.settings.bundle_max_bytes:
        raise Refusal(
            ErrorCode.BUNDLE_TOO_LARGE,
            evidence={"size_bytes": body.size_bytes, "max_bytes": rt.settings.bundle_max_bytes},
        )
    store = _store_of(rt)
    key = bundle_key(uow.org_id, app_id, body.digest)
    await lock_bundle_key(uow.conn, key)
    p = uow.principal
    inserted = await uow.conn.execute(
        _INSERT_BUNDLE,
        {
            "id": new_id("bdl"),
            "org": uow.org_id,
            "app": app_id,
            "digest": body.digest,
            "size": body.size_bytes,
            "commit": body.source_commit,
            "actor_kind": p.kind.value,
            "actor_id": p.subject,
            "via_agent": p.is_agent,
            "client_id": p.client_id,
        },
    )
    created = inserted.first() is not None
    params = {"org": uow.org_id, "app": app_id, "digest": body.digest}
    row = (await uow.conn.execute(_SELECT_BY_DIGEST, params)).mappings().one()
    if row["size_bytes"] != body.size_bytes:
        raise Refusal(
            ErrorCode.BUNDLE_DIGEST_MISMATCH,
            evidence={"size_bytes": body.size_bytes, "recorded_size_bytes": row["size_bytes"]},
        )
    upload = None
    if row["state"] == "pending":
        signed = await store.signed_url(
            key,
            method="PUT",
            content_length=body.size_bytes,
            sha256=body.digest.removeprefix(DIGEST_PREFIX),
        )
        upload = UploadTarget(
            method="PUT", url=signed.url, headers=dict(signed.headers), expires_at=signed.expires_at
        )
    return uow.reply(
        _out(row, upload),
        status=201 if created else 200,
        headers=_location(app_id, str(row["bundle_id"])),
    )


@router.post(
    "/apps/{app_id}/bundles/{bundle_id}/complete",
    response_model=BundleOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.NOT_FOUND,
        ErrorCode.FORBIDDEN,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.BUNDLE_NOT_UPLOADED,
        ErrorCode.BUNDLE_DIGEST_MISMATCH,
        ErrorCode.BUNDLE_MALFORMED,
        ErrorCode.MANIFEST_INVALID,
        ErrorCode.SECRET_IN_BUNDLE,
        ErrorCode.BUNDLE_TOO_LARGE,
    ),
)
async def complete_bundle(app_id: Id, bundle_id: Id, request: Request, uow: UserUoW) -> Response:
    """Check the uploaded object server side and store the bundle. A stored bundle answers 200."""
    rt = runtime_of(request)
    status = await _app_status_for_builder(uow, app_id)
    row = await _bundle(uow, app_id, bundle_id, lock=True)
    if row["state"] == "stored":
        return uow.reply(_out(row))
    if status != "active":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"status": status})
    store = _store_of(rt)
    key = bundle_key(uow.org_id, app_id, row["digest"])
    try:
        checked = await check_upload(
            store, key, size=row["size_bytes"], digest=row["digest"], limits=limits_of(rt.settings)
        )
    except BundleRejectedError as e:
        if e.code is ErrorCode.BUNDLE_DIGEST_MISMATCH:
            await _discard(store, key)
        raise Refusal(e.code, evidence={"bundle_id": bundle_id, **e.evidence}) from None
    manifest = checked.manifest.model_dump(mode="json", by_alias=True)
    stored = (
        (
            await uow.conn.execute(
                _STORE,
                {
                    "org": uow.org_id,
                    "id": bundle_id,
                    "manifest": json.dumps(manifest),
                    "manifest_digest": checked.manifest_digest,
                    "file_count": checked.file_count,
                },
            )
        )
        .mappings()
        .first()
    )
    if stored is None:
        return uow.reply(_out(await _bundle(uow, app_id, bundle_id)))
    await uow.audit(
        AuditAction.BUNDLE_STORED,
        target_kind="bundle",
        target_id=bundle_id,
        after={
            "app_id": app_id,
            "digest": stored["digest"],
            "size_bytes": stored["size_bytes"],
            "file_count": stored["file_count"],
            "manifest_digest": stored["manifest_digest"],
            "source_commit": stored["source_commit"],
        },
    )
    return uow.reply(_out(stored))


@router.get(
    "/apps/{app_id}/bundles/{bundle_id}",
    response_model=BundleOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN),
)
async def get_bundle(app_id: Id, bundle_id: Id, uow: UserUoW) -> BundleOut:
    await _app_status_for_builder(uow, app_id)
    return _out(await _bundle(uow, app_id, bundle_id))
