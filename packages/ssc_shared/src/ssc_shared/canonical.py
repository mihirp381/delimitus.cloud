"""RFC 8785 canonical JSON for manifest, snapshot and bundle digests.

Never used for the audit chain, which keeps its own frozen form (decision 012).
"""

import hashlib

import rfc8785

from ssc_contracts.manifest import Manifest

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | tuple[JsonValue, ...] | dict[str, JsonValue]
)


def canonical_bytes(value: JsonValue) -> bytes:
    """RFC 8785 bytes. Raises ``ValueError`` for integers beyond 2**53-1, NaN or infinity."""
    return rfc8785.dumps(value)


def canonical_digest(value: JsonValue) -> str:
    """``sha256:<hex>`` over the canonical bytes."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def manifest_digest(manifest: Manifest) -> str:
    """Digest of the normalised manifest: aliases as keys, defaults applied, sets sorted."""
    return canonical_digest(manifest.model_dump(mode="json", by_alias=True))
