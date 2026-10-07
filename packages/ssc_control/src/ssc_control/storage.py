"""The one blob store factory, ``blob_store``, and the ``SSC_BLOB_*`` settings it reads.

The API builds its store from ``api.settings`` (``api.routes.blobs.blob_store_for``), the worker
and the operator commands from the environment (``blob_store_from_env``); both call
``blob_store``, so every process of one deployment writes to one store. A test keeps the two
readers in step.

``none`` (the default) means no store. ``fs`` is the development store and is refused unless
``SSC_ENV`` is ``dev`` or ``test``. ``gcs`` is the bucket ``SSC_BLOB_BUCKET``, whose URLs are
signed through IAM ``signBlob`` as ``SSC_BLOB_SIGNER`` (decision 015).

``cell_stores_from_env`` is the other store family (SSC-013): one bucket per customer cell,
named by ``SSC_CELL_BUCKET_TEMPLATE`` with ``{cell}`` standing for the org's cell label, whose
URLs are signed as ``SSC_BLOB_SIGNER`` too. Access snapshots go there, so a cell reads its rules
from its own project, and so do the org's audit anchors (decision 012) and its source bundles
(decision 015 amendment), so app source never rests in the control plane. ``org_store`` and
``org_bundle_store`` make that choice for one org.
"""

import base64
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.db.bind import bound_org
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
_CELL_LABEL = text("select cell_label from ssc.org where id = :org")


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


def _signed_gcs_store(signer: str) -> Callable[[str], BlobStore]:
    def make(name: str) -> BlobStore:
        return GcsBlobStore(bucket_of(name), signer=IamSigner(signer))

    return make


def cell_stores(
    template: str, *, bucket: Callable[[str], BlobStore] | None = None, signer: str = ""
) -> CellStores:
    """Stores for ``template`` (one ``{cell}``), built once per label, their URLs signed as
    ``signer`` (none without it: such a store signs nothing). No I/O."""
    if template.count(CELL_PLACEHOLDER) != 1:
        raise StorageConfigError(f"{CELL_BUCKET_ENV} needs exactly one {CELL_PLACEHOLDER}")
    if signer and not _SERVICE_ACCOUNT.fullmatch(signer):
        raise StorageConfigError("SSC_BLOB_SIGNER must be a service account email")
    make = bucket or (_signed_gcs_store(signer) if signer else _gcs_store)
    built: dict[str, BlobStore] = {}

    def store(cell_label: str) -> BlobStore:
        label = check_cell_label(cell_label)
        if label not in built:
            built[label] = make(template.replace(CELL_PLACEHOLDER, label))
        return built[label]

    return store


def cell_stores_from_env(env: Mapping[str, str]) -> CellStores | None:
    """``SSC_CELL_BUCKET_TEMPLATE``, signed as ``SSC_BLOB_SIGNER`` when it is set; None when
    the template is unset."""
    template = env.get(CELL_BUCKET_ENV, "")
    return cell_stores(template, signer=env.get("SSC_BLOB_SIGNER", "")) if template else None


async def org_store(
    engine: AsyncEngine,
    org_id: str,
    *,
    blob_store: BlobStore | None,
    cell_stores: CellStores | None,
) -> BlobStore | None:
    """Where the org's snapshots and audit anchors go: its cell's bucket when ``cell_stores`` is
    set, else the one blob store (None when neither is configured)."""
    if cell_stores is None:
        return blob_store
    async with bound_org(engine, org_id) as conn:
        label = (await conn.execute(_CELL_LABEL, {"org": org_id})).scalar_one()
    return cell_stores(str(label))


async def org_bundle_store(
    engine: AsyncEngine,
    org_id: str,
    *,
    blob_store: BlobStore | None,
    cell_stores: CellStores | None,
) -> BlobStore | None:
    """Where the org's source bundles are written, signed, read and collected: its cell's
    bucket when ``cell_stores`` is set, so nothing writes ``bundles/`` to the control store;
    else the one blob store (development and tests; None when neither is configured). The
    keys are the same in both (``deploy.bundles.bundle_key``)."""
    return await org_store(engine, org_id, blob_store=blob_store, cell_stores=cell_stores)
