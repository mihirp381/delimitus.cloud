"""The one blob store factory, ``blob_store``, and the ``SSC_BLOB_*`` settings it reads.

The API builds its store from ``api.settings`` (``api.routes.blobs.blob_store_for``), the worker
and the operator commands from the environment (``blob_store_from_env``); both call
``blob_store``, so every process of one deployment writes to one store. A test keeps the two
readers in step.

``none`` (the default) means no store. ``fs`` is the development store and is refused unless
``SSC_ENV`` is ``dev`` or ``test``; bucket bindings arrive with SSC-013.
"""

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock

BACKEND_ENV: Final = "SSC_BLOB_BACKEND"
FS_ENVIRONMENTS: Final = frozenset({"dev", "test"})
BLOBS_PATH: Final = "/blobs"
"""Where the API serves the filesystem store's signed URLs."""
DEFAULT_PUBLIC_URL: Final = "https://api.delimitus.com"


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


def blob_store(  # noqa: PLR0913  (keyword-only)
    backend: str,
    *,
    environment: str,
    root: str,
    keys: Mapping[str, bytes],
    kid: str,
    public_url: str,
) -> BlobStore | None:
    """The store ``backend`` names. No I/O."""
    match backend:
        case "none":
            return None
        case "fs":
            _refuse_fs_outside_dev(environment)
            if not root:
                raise StorageConfigError("SSC_BLOB_BACKEND=fs needs SSC_BLOB_ROOT")
            try:
                signer = UrlSigner(keys, active=kid, clock=SystemClock())
            except ValueError as exc:
                raise StorageConfigError(str(exc)) from exc
            base = public_url.rstrip("/") + BLOBS_PATH
            return FsBlobStore(Path(root), signer=signer, base_url=base)
        case other:
            raise StorageConfigError(f"unknown {BACKEND_ENV} {other!r}")


def blob_store_from_env(env: Mapping[str, str]) -> BlobStore | None:
    """``blob_store`` over ``SSC_BLOB_*``, ``SSC_ENV`` and ``SSC_API_PUBLIC_URL``. No I/O; the
    signing keys are read only for a store that signs."""
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
    )
