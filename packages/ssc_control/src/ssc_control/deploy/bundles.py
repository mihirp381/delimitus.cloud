"""Where a bundle lives in the blob store, and what ``complete`` checks before it is stored.

The server never trusts the client's packing or scan: it re-reads the stored object, checks its
size and sha256, inspects the tar (``ssc_bundle.tarcheck``), reads ``ssc.toml`` from the bundle
itself and repeats the secret scan. Every refusal carries an ``ErrorCode`` and log-only evidence;
evidence never holds a secret value, masked or not.
"""

import asyncio
import hashlib
import hmac
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Final

from ssc_bundle.limits import BundleMalformedError, BundleTooLargeError, Limits
from ssc_bundle.secrets import MAX_SCAN_BYTES, allowed_values, scan
from ssc_bundle.tarcheck import inspect, iter_files
from ssc_contracts.errors import ErrorCode
from ssc_contracts.manifest import Manifest, ManifestError, default_manifest, load_manifest
from ssc_shared.blobstore import BlobCorruptError, BlobNotFoundError, BlobStore, check_key
from ssc_shared.canonical import manifest_digest

DIGEST_PREFIX: Final = "sha256:"
MAX_EVIDENCE_FINDINGS: Final = 20


def bundle_key(org_id: str, app_id: str, digest: str) -> str:
    """``bundles/{org}/{app}/sha256/{hex}.tar.gz``: content-addressed per org and app."""
    hex_ = digest.removeprefix(DIGEST_PREFIX)
    if not digest.startswith(DIGEST_PREFIX) or len(hex_) != 64:
        raise ValueError(f"not a sha256 digest: {digest!r}")
    return check_key(f"bundles/{org_id}/{app_id}/sha256/{hex_}.tar.gz")


class BundleRejectedError(Exception):
    """The upload is refused with ``code``; ``evidence`` is for the log only."""

    def __init__(self, code: ErrorCode, evidence: Mapping[str, object]) -> None:
        super().__init__(code.value)
        self.code = code
        self.evidence = dict(evidence)


@dataclass(frozen=True, slots=True)
class CheckedBundle:
    manifest: Manifest
    manifest_digest: str
    file_count: int
    unpacked_bytes: int


async def check_upload(
    store: BlobStore, key: str, *, size: int, digest: str, limits: Limits
) -> CheckedBundle:
    """Re-read the stored object and run every server-side check; raises ``BundleRejectedError``."""
    want = digest.removeprefix(DIGEST_PREFIX)
    try:
        info = await store.stat(key)
    except BlobCorruptError:
        raise BundleRejectedError(ErrorCode.BUNDLE_DIGEST_MISMATCH, {"reason": "corrupt"}) from None
    if info is None:
        raise BundleRejectedError(ErrorCode.BUNDLE_NOT_UPLOADED, {"key": key})
    if info.size != size or not hmac.compare_digest(info.sha256, want):
        raise BundleRejectedError(
            ErrorCode.BUNDLE_DIGEST_MISMATCH,
            {"size": size, "stored_size": info.size, "stored_sha256": info.sha256},
        )
    if size > limits.max_bytes:
        raise BundleRejectedError(ErrorCode.BUNDLE_TOO_LARGE, {"reason": "bytes", "size": size})
    with tempfile.TemporaryFile() as tmp:
        await _download(store, key, tmp, size=size, sha256=want)
        return await asyncio.to_thread(_inspect, tmp, limits)


async def _download(store: BlobStore, key: str, out: IO[bytes], *, size: int, sha256: str) -> None:
    h = hashlib.sha256()
    n = 0
    try:
        async for chunk in store.get(key):
            n += len(chunk)
            if n > size:
                break
            h.update(chunk)
            await asyncio.to_thread(out.write, chunk)
    except BlobNotFoundError:
        raise BundleRejectedError(ErrorCode.BUNDLE_NOT_UPLOADED, {"key": key}) from None
    except BlobCorruptError:
        raise BundleRejectedError(ErrorCode.BUNDLE_DIGEST_MISMATCH, {"reason": "corrupt"}) from None
    if n != size or not hmac.compare_digest(h.hexdigest(), sha256):
        raise BundleRejectedError(ErrorCode.BUNDLE_DIGEST_MISMATCH, {"reason": "read_back"})


def _inspect(tmp: IO[bytes], limits: Limits) -> CheckedBundle:
    try:
        tmp.seek(0)
        report = inspect(tmp, limits)
        manifest = _manifest(report.manifest_text)
        tmp.seek(0)
        findings = scan(iter_files(tmp, limits, MAX_SCAN_BYTES), allowed_values(manifest))
    except BundleTooLargeError as e:
        raise BundleRejectedError(
            ErrorCode.BUNDLE_TOO_LARGE, {"reason": e.reason, "path": e.path}
        ) from None
    except BundleMalformedError as e:
        raise BundleRejectedError(
            ErrorCode.BUNDLE_MALFORMED, {"reason": e.reason, "path": e.path}
        ) from None
    blocking = [f for f in findings if f.blocking]
    if blocking:
        shown = [{"path": f.path, "line": f.line, "rule": f.rule} for f in blocking]
        raise BundleRejectedError(
            ErrorCode.SECRET_IN_BUNDLE,
            {"count": len(blocking), "findings": shown[:MAX_EVIDENCE_FINDINGS]},
        )
    return CheckedBundle(
        manifest, manifest_digest(manifest), report.file_count, report.unpacked_bytes
    )


def _manifest(text: bytes | None) -> Manifest:
    if text is None:
        return default_manifest()
    try:
        return load_manifest(text)
    except ManifestError as e:
        problems = [{"line": p.line, "column": p.column, "field": p.field} for p in e.problems]
        raise BundleRejectedError(ErrorCode.MANIFEST_INVALID, {"problems": problems}) from None
