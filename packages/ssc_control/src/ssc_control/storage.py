"""The blob store the worker and the operator commands use, read from the same ``SSC_BLOB_*``
settings as the API (``api.settings``, ``api.routes.blobs.blob_store_for``), so every process
of one deployment writes to one store. A test keeps the two readers in step.

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


def _signing_keys(raw: str) -> dict[str, bytes]:
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


def blob_store_from_env(env: Mapping[str, str]) -> BlobStore | None:
    """The store ``SSC_BLOB_BACKEND`` names. No I/O."""
    match env.get(BACKEND_ENV, "none"):
        case "none":
            return None
        case "fs":
            if env.get("SSC_ENV") not in FS_ENVIRONMENTS:
                raise StorageConfigError(
                    f"the filesystem blob store runs only in {sorted(FS_ENVIRONMENTS)}, "
                    f"not {env.get('SSC_ENV', 'prod')!r}"
                )
            root = env.get("SSC_BLOB_ROOT", "")
            if not root:
                raise StorageConfigError("SSC_BLOB_BACKEND=fs needs SSC_BLOB_ROOT")
            keys = _signing_keys(env.get("SSC_BLOB_SIGNING_KEYS", "{}"))
            try:
                signer = UrlSigner(
                    keys, active=env.get("SSC_BLOB_SIGNING_KID", ""), clock=SystemClock()
                )
            except ValueError as exc:
                raise StorageConfigError(str(exc)) from exc
            base = env.get("SSC_API_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/") + BLOBS_PATH
            return FsBlobStore(Path(root), signer=signer, base_url=base)
        case other:
            raise StorageConfigError(f"unknown {BACKEND_ENV} {other!r}")
