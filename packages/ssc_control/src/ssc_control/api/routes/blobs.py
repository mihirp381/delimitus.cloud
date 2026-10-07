"""The filesystem store's signed URLs, served by the API in dev and test only.

Not in the OpenAPI document and mounted only for an ``FsBlobStore``; ``create_app`` refuses that
store outside ``dev`` and ``test``. The signature is the credential, so there is no bearer token.
A PUT stores exactly the signed length and sha256 and stops reading at the length. Objects are
stored and served as ``application/octet-stream`` attachments, never as a type a browser renders.
Deployed cells use the bucket's own signed URLs (``SSC_BLOB_BACKEND=gcs``) instead.
"""

from typing import Final

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from ssc_contracts.errors import ErrorCode
from ssc_control.api.problems import Refusal
from ssc_control.api.runtime import runtime_of
from ssc_control.api.settings import Settings
from ssc_control.storage import BLOBS_PATH, CellStores, blob_store, cell_stores
from ssc_control.storage import check_fs_allowed as check_fs_environment
from ssc_shared.blobstore import (
    DEFAULT_CONTENT_TYPE,
    BlobCorruptError,
    BlobKeyError,
    BlobMismatchError,
    BlobNotFoundError,
    BlobStore,
    BlobTooLargeError,
)
from ssc_shared.blobstore_fs import FsBlobStore, SignedUrlError

PREFIX: Final = BLOBS_PATH

router = APIRouter(prefix=PREFIX, include_in_schema=False)


def blob_store_for(settings: Settings) -> BlobStore | None:
    """``storage.blob_store`` over the API's settings. No I/O."""
    return blob_store(
        settings.blob_backend,
        environment=settings.environment,
        root=settings.blob_root,
        keys=settings.blob_signing_keys,
        kid=settings.blob_signing_kid,
        public_url=settings.public_url,
        bucket=settings.blob_bucket,
        signer=settings.blob_signer,
    )


def cell_stores_for(settings: Settings) -> CellStores | None:
    """``storage.cell_stores`` over the API's settings, signed as ``blob_signer``; None without
    ``cell_bucket_template``. No I/O."""
    template = settings.cell_bucket_template
    return cell_stores(template, signer=settings.blob_signer) if template else None


def check_fs_allowed(store: BlobStore | None, settings: Settings) -> None:
    """``storage.check_fs_allowed``: also refuses a filesystem store passed to ``create_app``."""
    check_fs_environment(store, settings.environment)


def _store(request: Request) -> FsBlobStore:
    store = runtime_of(request).blob_store
    if not isinstance(store, FsBlobStore):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"reason": "no_fs_blob_store"})
    return store


def _params(request: Request) -> dict[str, str]:
    query = request.query_params
    if len(query.multi_items()) != len(query):
        raise Refusal(ErrorCode.UPLOAD_URL_INVALID, evidence={"reason": "repeated_parameter"})
    return dict(query)


@router.put("/{key:path}", status_code=201)
async def put_blob(key: str, request: Request) -> Response:
    store = _store(request)
    try:
        info = await store.accept_put(
            key, _params(request), request.stream(), content_type=DEFAULT_CONTENT_TYPE
        )
    except SignedUrlError as e:
        raise Refusal(ErrorCode.UPLOAD_URL_INVALID, evidence={"reason": e.reason}) from None
    except BlobKeyError:
        raise Refusal(ErrorCode.UPLOAD_URL_INVALID, evidence={"reason": "bad_key"}) from None
    except BlobTooLargeError:
        raise Refusal(ErrorCode.BUNDLE_TOO_LARGE, evidence={"reason": "body_too_long"}) from None
    except BlobMismatchError as e:
        raise Refusal(ErrorCode.BUNDLE_DIGEST_MISMATCH, evidence={"reason": str(e)}) from None
    return Response(status_code=201, headers={"ETag": f'"{info.sha256}"'})


@router.get("/{key:path}")
async def get_blob(key: str, request: Request) -> StreamingResponse:
    store = _store(request)
    try:
        info, body = await store.open_get(key, _params(request))
    except SignedUrlError as e:
        raise Refusal(ErrorCode.UPLOAD_URL_INVALID, evidence={"reason": e.reason}) from None
    except BlobKeyError:
        raise Refusal(ErrorCode.UPLOAD_URL_INVALID, evidence={"reason": "bad_key"}) from None
    except BlobNotFoundError:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"key": key}) from None
    except BlobCorruptError:
        raise Refusal(ErrorCode.INTERNAL, evidence={"reason": "corrupt", "key": key}) from None
    return StreamingResponse(
        body,
        media_type=DEFAULT_CONTENT_TYPE,
        headers={
            "Content-Length": str(info.size),
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
