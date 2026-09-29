"""What ``ssc deploy`` runs before any network call: manifest, pack, then the secret scan.

``prepare`` raises (``ManifestError``, ``BundleError``, ``SecretFoundError``) before anything
leaves the machine; ``ship`` hands a clean bundle to the caller's uploader and nothing else.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ssc_bundle.limits import DEFAULT_LIMITS, BundleError, Limits
from ssc_bundle.pack import PackedBundle, pack
from ssc_bundle.secrets import MAX_SCAN_BYTES, Finding, allowed_values, scan
from ssc_bundle.tarcheck import iter_files
from ssc_contracts.manifest import MANIFEST_FILE, Manifest, default_manifest, load_manifest
from ssc_shared.canonical import manifest_digest


@dataclass(frozen=True, slots=True)
class Prepared:
    bundle: PackedBundle
    manifest: Manifest
    manifest_digest: str
    warnings: tuple[Finding, ...]


class SecretFoundError(BundleError):
    """Blocking findings, masked. The packed file has been deleted."""

    def __init__(self, findings: tuple[Finding, ...]) -> None:
        first = findings[0]
        super().__init__(
            "secret", f"{first.rule} {first.masked} at line {first.line}", path=first.path
        )
        self.findings = findings


def read_manifest(root: Path) -> Manifest:
    path = root / MANIFEST_FILE
    return load_manifest(path.read_bytes()) if path.is_file() else default_manifest()


def prepare(root: Path, dest: Path, *, limits: Limits = DEFAULT_LIMITS) -> Prepared:
    manifest = read_manifest(root)
    bundle = pack(root, dest, limits=limits)
    with dest.open("rb") as f:
        findings = scan(iter_files(f, limits, MAX_SCAN_BYTES), allowed_values(manifest))
    blocking = tuple(x for x in findings if x.blocking)
    if blocking:
        dest.unlink(missing_ok=True)
        raise SecretFoundError(blocking)
    warnings = tuple(x for x in findings if not x.blocking)
    return Prepared(bundle, manifest, manifest_digest(manifest), warnings)


def ship[T](
    root: Path, dest: Path, upload: Callable[[Prepared], T], *, limits: Limits = DEFAULT_LIMITS
) -> T:
    """``prepare``, then ``upload`` only if nothing was refused."""
    return upload(prepare(root, dest, limits=limits))
