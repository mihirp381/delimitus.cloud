"""Bundle caps (settings-adjustable) and the refusals every bundle module raises."""

from dataclasses import dataclass
from typing import Final

MIB: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Limits:
    max_bytes: int = 100 * MIB
    """Compressed ``.tar.gz`` size."""
    max_unpacked_bytes: int = 500 * MIB
    """Sum of the file sizes."""
    max_files: int = 20_000
    """Entries in the archive."""


DEFAULT_LIMITS: Final = Limits()


class BundleError(Exception):
    """A refused bundle. ``reason`` is a stable slug; ``path`` names the entry when there is one."""

    def __init__(self, reason: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message if path is None else f"{path}: {message}")
        self.reason = reason
        self.path = path


class BundleTooLargeError(BundleError):
    """Over a cap: ``reason`` is ``bytes``, ``unpacked`` or ``files``."""


class BundleMalformedError(BundleError):
    """An archive the server will not accept (links, ``..``, ``.env`` files, not a tar.gz)."""
