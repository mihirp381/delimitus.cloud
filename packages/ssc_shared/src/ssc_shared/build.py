"""The build status types, and what the control plane asks a cell to build (SSC-015).

The control plane (``ssc_control.deploy``) decides what to build; the cell agent (``ssc_agent``)
runs it on the cell's Cloud Build. Both speak these types, so they live below both.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol, cast

from ssc_contracts.packages import APPROVED_PACKAGES

BUILD_ID: Final = re.compile(r"bld_[a-z0-9]{20}")
MAX_REF_CHARS: Final = 512

_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


@dataclass(frozen=True, slots=True)
class Running:
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Succeeded:
    image_digest: str
    scan_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.image_digest):
            raise ValueError(f"images are pinned by sha256 digest: {self.image_digest!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Failed:
    """``message`` is for the build log; it never reaches an audit row."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not _CODE.fullmatch(self.code):
            raise ValueError(f"not a reason code: {self.code!r}")


type BuildStatus = Running | Succeeded | Failed


class BuildDriverError(Exception):
    """The builder refused or failed a call; the job retries until its deadline."""


class BuildNotFoundError(BuildDriverError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class CellBuild:
    """One build in a cell. ``bundle_url`` is a signed GET for the bundle that lives at most 10
    minutes; it is a credential, so it is never logged. ``start`` is the manifest's start
    command, or None for Railpack's own. ``system_packages`` are the packages from the platform
    package list the build installs (``ssc_contracts.packages``); no other is accepted."""

    build_id: str
    bundle_url: str = field(repr=False)
    bundle_sha256: str
    public_env: Mapping[str, str] = field(default_factory=dict[str, str])
    start: str | None = None
    system_packages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not BUILD_ID.fullmatch(self.build_id):
            raise ValueError(f"not a build id: {self.build_id!r}")
        if not self.bundle_url.startswith("https://"):
            raise ValueError("the bundle URL must be https")
        if not _SHA256.fullmatch(self.bundle_sha256):
            raise ValueError("bundle_sha256 must be 64 lowercase hex characters")
        bad = [k for k in self.public_env if not _ENV_NAME.fullmatch(k)]
        if bad:
            raise ValueError(f"not environment variable names: {bad}")
        unlisted = sorted(set(self.system_packages) - APPROVED_PACKAGES)
        if unlisted:
            raise ValueError(f"not on the platform package list: {unlisted}")
        object.__setattr__(self, "public_env", MappingProxyType(dict(self.public_env)))
        object.__setattr__(self, "system_packages", tuple(sorted(set(self.system_packages))))


class CellBuilder(Protocol):
    """What the agent runs builds with. ``start`` finds the build already started for the same
    ``build_id``; ``poll`` raises ``BuildNotFoundError`` for an unknown reference."""

    async def start(self, build: CellBuild) -> str: ...

    async def poll(self, ref: str) -> BuildStatus: ...


def build_to_wire(build: CellBuild) -> dict[str, object]:
    return {
        "build_id": build.build_id,
        "bundle_url": build.bundle_url,
        "bundle_sha256": build.bundle_sha256,
        "public_env": dict(build.public_env),
        "start": build.start,
        "system_packages": list(build.system_packages),
    }


def build_from_wire(body: Mapping[str, Any]) -> CellBuild:
    """Raises ``ValueError`` for anything malformed. A build without ``system_packages`` (from a
    control plane older than SSC-093) installs none."""
    try:
        start = body["start"]
        packages: object = body.get("system_packages", [])
        if not isinstance(packages, list):
            raise TypeError("system_packages must be a list")
        return CellBuild(
            build_id=_str(body["build_id"]),
            bundle_url=_str(body["bundle_url"]),
            bundle_sha256=_str(body["bundle_sha256"]),
            public_env=_str_map(body["public_env"]),
            start=None if start is None else _str(start),
            system_packages=tuple(_str(p) for p in cast("list[object]", packages)),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed build: {exc}") from None


def status_to_wire(status: BuildStatus) -> dict[str, object]:
    match status:
        case Running():
            return {"state": "running"}
        case Succeeded(image_digest=digest, scan_refs=refs):
            return {"state": "succeeded", "image_digest": digest, "scan_refs": list(refs)}
        case Failed(code=code, message=message):
            return {"state": "failed", "code": code, "message": message}


def status_from_wire(body: Mapping[str, Any]) -> BuildStatus:
    """Raises ``ValueError`` for anything malformed."""
    try:
        match body["state"]:
            case "running":
                return Running()
            case "succeeded":
                refs: object = body["scan_refs"]
                if not isinstance(refs, list):
                    raise TypeError("scan_refs must be a list")
                return Succeeded(
                    image_digest=_str(body["image_digest"]),
                    scan_refs=tuple(_str(r) for r in cast("list[object]", refs)),
                )
            case "failed":
                return Failed(code=_str(body["code"]), message=_str(body["message"]))
            case other:
                raise ValueError(f"unknown build state {other!r}")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed build status: {exc}") from None


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError(f"expected an object, got {type(value).__name__}")
    items = cast("dict[object, object]", value).items()
    return {_str(k): _str(v) for k, v in items}
