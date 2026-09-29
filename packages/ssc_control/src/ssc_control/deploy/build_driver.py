"""The build driver seam: one stored bundle in, one image for one environment out (SSC-016).

Cloud Build with Railpack waits for SSC-015. Until then ``FakeBuildDriver`` implements the
protocol, and every implementation must pass ``conformance/ssc_conformance/contracts/
build_driver.py``. Failure codes are Delimitus' ``contracts.intake-build-reasons`` build codes
(``BUILD_REASONS``) plus the two this seam adds, so SSC-015 can reuse its fixtures.

Each environment builds separately from the same source (C08): public build values differ per
environment, so the image does too.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, Protocol, get_args

from ssc_contracts.manifest import Manifest
from ssc_shared.canonical import canonical_bytes

BUILD_REASONS: Final = frozenset(
    {
        "BUILD_DEPENDENCY_UNRESOLVED",
        "BUILD_PRIVATE_REGISTRY",
        "BUILD_NO_ENTRYPOINT",
        "BUILD_EXITED_NONZERO",
    }
)
BUILD_TIMED_OUT: Final = "BUILD_TIMED_OUT"
BUILD_DRIVER_ERROR: Final = "BUILD_DRIVER_ERROR"
MAX_REF_CHARS: Final = 512

_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildRequest:
    """``build_id`` names the attempt: starting the same id twice is one build."""

    build_id: str
    org_id: str
    app_id: str
    env_name: str
    bundle_key: str
    source_digest: str
    manifest: Manifest
    public_env: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.source_digest):
            raise ValueError(f"not a sha256 digest: {self.source_digest!r}")
        object.__setattr__(self, "public_env", MappingProxyType(dict(self.public_env)))


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


class BuildDriver(Protocol):
    async def start(self, request: BuildRequest) -> str:
        """Start the build, or find the one already started for ``request.build_id``; the
        builder's reference, at most ``MAX_REF_CHARS`` characters."""
        ...

    async def poll(self, ref: str) -> BuildStatus:
        """Where the build is. A terminal status never changes; an unknown ``ref`` raises
        ``BuildNotFoundError``."""
        ...


def fake_image_digest(source_digest: str, env_name: str, public_env: Mapping[str, str]) -> str:
    """``sha256("image:" + source_digest + env_name + canonical(public_env))``."""
    body = b"image:" + source_digest.encode() + env_name.encode()
    body += canonical_bytes(dict(sorted(public_env.items())))
    return "sha256:" + hashlib.sha256(body).hexdigest()


Method = Literal["start", "poll"]
METHODS: Final[tuple[str, ...]] = get_args(Method)


@dataclass(slots=True)
class _FakeBuild:
    request: BuildRequest
    polls_left: int
    result: Succeeded | Failed


class FakeBuildDriver(BuildDriver):
    """In memory. A build reports ``Running`` for ``polls`` polls, then its result: the digest
    from :func:`fake_image_digest`, or the failure set with :meth:`fail` for its source."""

    def __init__(self, *, polls: int = 0) -> None:
        self.requests: list[BuildRequest] = []
        self._polls = polls
        self._builds: dict[str, _FakeBuild] = {}
        self._refs: dict[str, str] = {}
        self._failing: dict[str, Failed] = {}
        self._errors: dict[Method, list[Exception]] = {}

    def fail(self, source_digest: str, code: str, message: str = "the build failed") -> None:
        """Builds of this source started from now on fail with ``code``."""
        self._failing[source_digest] = Failed(code=code, message=message)

    def fail_next(self, method: Method, exc: Exception) -> None:
        """The next call to ``method`` raises ``exc`` and changes nothing."""
        self._errors.setdefault(method, []).append(exc)

    def _raise_injected(self, method: Method) -> None:
        pending = self._errors.get(method)
        if pending:
            raise pending.pop(0)

    async def start(self, request: BuildRequest) -> str:
        self._raise_injected("start")
        ref = self._refs.get(request.build_id)
        if ref is not None:
            return ref
        ref = f"fake-build-{len(self._builds) + 1:05d}"
        result = self._failing.get(request.source_digest) or Succeeded(
            image_digest=fake_image_digest(
                request.source_digest, request.env_name, request.public_env
            ),
            scan_refs=(f"fake-scan:{request.build_id}",),
        )
        self._builds[ref] = _FakeBuild(request, self._polls, result)
        self._refs[request.build_id] = ref
        self.requests.append(request)
        return ref

    async def poll(self, ref: str) -> BuildStatus:
        self._raise_injected("poll")
        build = self._builds.get(ref)
        if build is None:
            raise BuildNotFoundError(ref)
        if build.polls_left > 0:
            build.polls_left -= 1
            return Running()
        return build.result
