"""The filesystem store's signed URLs, served by the API in dev and test only.

Not in the OpenAPI document and mounted only for an ``FsBlobStore``; ``create_app`` refuses that
store outside ``dev`` and ``test``. The signature is the credential, so there is no bearer token.
A PUT stores exactly the signed length and sha256 and stops reading at the length. Objects are
stored and served as ``application/octet-stream`` attachments, never as a type a browser renders.
Deployed cells use the bucket's own signed URLs (SSC-013) instead.
"""

from pathlib import Path
from typing import Final

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from ssc_contracts.errors import ErrorCode
from ssc_control.api.problems import Refusal
from ssc_control.api.runtime import runtime_of
from ssc_control.api.settings import Settings
from ssc_shared.blobstore import (
    DEFAULT_CONTENT_TYPE,
    BlobCorruptError,
    BlobKeyError,
    BlobMismatchError,
    BlobNotFoundError,
    BlobStore,
    BlobTooLargeError,
)
from ssc_shared.blobstore_fs import FsBlobStore, SignedUrlError, UrlSigner
from ssc_shared.clock import SystemClock

PREFIX: Final = "/blobs"
FS_ENVIRONMENTS: Final = frozenset({"dev", "test"})

router = APIRouter(prefix=PREFIX, include_in_schema=False)


def blob_store_for(settings: Settings) -> BlobStore | None:
    """The store ``settings.blob_backend`` names. No I/O."""
    match settings.blob_backend:
        case "none":
            return None
        case "fs":
            if not settings.blob_root:
                raise ValueError("blob_backend fs needs blob_root")
            signer = UrlSigner(
                settings.blob_signing_keys, active=settings.blob_signing_kid, clock=SystemClock()
            )
            base = settings.public_url.rstrip("/") + PREFIX
            return FsBlobStore(Path(settings.blob_root), signer=signer, base_url=base)
        case other:
            raise ValueError(f"unknown blob_backend {other!r}")


def check_fs_allowed(store: BlobStore | None, settings: Settings) -> None:
    if isinstance(store, FsBlobStore) and settings.environment not in FS_ENVIRONMENTS:
        raise ValueError(
            f"the filesystem blob store runs only in {sorted(FS_ENVIRONMENTS)}, "
            f"not {settings.environment!r}"
        )


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
