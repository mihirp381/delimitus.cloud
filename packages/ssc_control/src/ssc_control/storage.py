"""The one blob store factory, ``blob_store``, and the ``SSC_BLOB_*`` settings it reads.

The API builds its store from ``api.settings`` (``api.routes.blobs.blob_store_for``), the worker
and the operator commands from the environment (``blob_store_from_env``); both call
``blob_store``, so every process of one deployment writes to one store. A test keeps the two
readers in step.

``none`` (the default) means no store. ``fs`` is the development store and is refused unless
``SSC_ENV`` is ``dev`` or ``test``. ``gcs`` is the bucket ``SSC_BLOB_BUCKET``, whose URLs are
signed through IAM ``signBlob`` as ``SSC_BLOB_SIGNER`` (decision 015): one bucket until placement
(which cell holds an org) has its ticket.

``cell_stores_from_env`` is the other store family (SSC-013): one bucket per customer cell,
named by ``SSC_CELL_BUCKET_TEMPLATE`` with ``{cell}`` standing for the org's cell label. Access
snapshots go there, so a cell reads its rules from its own project.
"""

import base64
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, cast

from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.blobstore_gcs import GcsBlobStore, IamSigner, bucket_of
from ssc_shared.clock import SystemClock
from ssc_shared.hosts import check_cell_label

BACKEND_ENV: Final = "SSC_BLOB_BACKEND"
CELL_BUCKET_ENV: Final = "SSC_CELL_BUCKET_TEMPLATE"
CELL_PLACEHOLDER: Final = "{cell}"

CellStores = Callable[[str], BlobStore]
"""A cell label to that cell's bucket store."""
FS_ENVIRONMENTS: Final = frozenset({"dev", "test"})
BLOBS_PATH: Final = "/blobs"
"""Where the API serves the filesystem store's signed URLs."""
DEFAULT_PUBLIC_URL: Final = "https://api.delimitus.com"
_BUCKET = re.compile(r"[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]")
_SERVICE_ACCOUNT = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com"
)


class StorageConfigError(ValueError):
    pass


def signing_keys(raw: str) -> dict[str, bytes]:
    """``SSC_BLOB_SIGNING_KEYS``: a JSON object of key id to base64 key."""
    try:
        keys: object = json.loads(raw)
    except ValueError as exc:
        raise StorageConfigError("SSC_BLOB_SIGNING_KEYS must be JSON") from exc
    if not isinstance(keys, dict):
        raise StorageConfigError("SSC_BLOB_SIGNING_KEYS must be a JSON object")
    try:
        return {
            str(kid): base64.b64decode(str(value), validate=True)
            for kid, value in cast(dict[object, object], keys).items()
        }
    except ValueError as exc:
        raise StorageConfigError("SSC_BLOB_SIGNING_KEYS values must be base64") from exc


def check_fs_allowed(store: BlobStore | None, environment: str) -> None:
    """Refuse a filesystem store, however it was built, outside ``dev`` and ``test``."""
    if isinstance(store, FsBlobStore):
        _refuse_fs_outside_dev(environment)


def _refuse_fs_outside_dev(environment: str) -> None:
    if environment not in FS_ENVIRONMENTS:
        raise StorageConfigError(
            f"the filesystem blob store runs only in {sorted(FS_ENVIRONMENTS)}, not {environment!r}"
        )


def gcs_store(bucket: str, signer: str) -> BlobStore:
    """The bucket store with IAM-signed URLs. Builds a storage client, so it reads credentials."""
    return GcsBlobStore(bucket_of(bucket), signer=IamSigner(signer))


def blob_store(  # noqa: PLR0913  (keyword-only)
    backend: str,
    *,
    environment: str,
    root: str,
    keys: Mapping[str, bytes],
    kid: str,
    public_url: str,
    bucket: str = "",
    signer: str = "",
) -> BlobStore | None:
    """The store ``backend`` names. No I/O except the ``gcs`` client's credential lookup."""
    match backend:
        case "none":
            return None
        case "fs":
            _refuse_fs_outside_dev(environment)
            if not root:
                raise StorageConfigError("SSC_BLOB_BACKEND=fs needs SSC_BLOB_ROOT")
            try:
                url_signer = UrlSigner(keys, active=kid, clock=SystemClock())
            except ValueError as exc:
                raise StorageConfigError(str(exc)) from exc
            base = public_url.rstrip("/") + BLOBS_PATH
            return FsBlobStore(Path(root), signer=url_signer, base_url=base)
        case "gcs":
            if not _BUCKET.fullmatch(bucket):
                raise StorageConfigError(
                    "SSC_BLOB_BACKEND=gcs needs SSC_BLOB_BUCKET, a bucket name"
                )
            if not _SERVICE_ACCOUNT.fullmatch(signer):
                raise StorageConfigError(
                    "SSC_BLOB_BACKEND=gcs needs SSC_BLOB_SIGNER, a service account email"
                )
            return gcs_store(bucket, signer)
        case other:
            raise StorageConfigError(f"unknown {BACKEND_ENV} {other!r}")


def blob_store_from_env(env: Mapping[str, str]) -> BlobStore | None:
    """``blob_store`` over ``SSC_BLOB_*``, ``SSC_ENV`` and ``SSC_API_PUBLIC_URL``. The ``fs``
    signing keys are read only for that store."""
    backend = env.get(BACKEND_ENV, "none")
    if backend == "none":
        return None
    return blob_store(
        backend,
        environment=env.get("SSC_ENV", "prod"),
        root=env.get("SSC_BLOB_ROOT", ""),
        keys=signing_keys(env.get("SSC_BLOB_SIGNING_KEYS", "{}")) if backend == "fs" else {},
        kid=env.get("SSC_BLOB_SIGNING_KID", ""),
        public_url=env.get("SSC_API_PUBLIC_URL", DEFAULT_PUBLIC_URL),
        bucket=env.get("SSC_BLOB_BUCKET", ""),
        signer=env.get("SSC_BLOB_SIGNER", ""),
    )


def _gcs_store(name: str) -> BlobStore:
    return GcsBlobStore(bucket_of(name))


def cell_stores(template: str, *, bucket: Callable[[str], BlobStore] | None = None) -> CellStores:
    """Stores for ``template`` (one ``{cell}``), built once per label. No I/O."""
    if template.count(CELL_PLACEHOLDER) != 1:
        raise StorageConfigError(f"{CELL_BUCKET_ENV} needs exactly one {CELL_PLACEHOLDER}")
    make = bucket or _gcs_store
    built: dict[str, BlobStore] = {}

    def store(cell_label: str) -> BlobStore:
        label = check_cell_label(cell_label)
        if label not in built:
            built[label] = make(template.replace(CELL_PLACEHOLDER, label))
        return built[label]

    return store


def cell_stores_from_env(env: Mapping[str, str]) -> CellStores | None:
    """``SSC_CELL_BUCKET_TEMPLATE``, or None when it is unset."""
    template = env.get(CELL_BUCKET_ENV, "")
    return cell_stores(template) if template else None
